from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from runrail.api.crud import create_run, get_or_404
from runrail.api.routes_workflows import _ensure_workflow_runnable
from runrail.api.ws import manager as ws_manager
from runrail.daybuckets import day_bounds, local_date_expr, offset_segments, resolve_zone
from runrail.db import get_db
from runrail.models import (
    Artifact,
    RunStatus,
    TaskRun,
    TaskRunStatus,
    TriggerType,
    Workflow,
    WorkflowRun,
    now,
)
from runrail.schemas import TaskRunOut, WorkflowRunDetail, WorkflowRunOut

router = APIRouter(prefix="/api")


@router.get("/runs", response_model=list[WorkflowRunOut])
def list_runs(status: RunStatus | None = None, workflow_id: int | None = None,
              day: date | None = None, tz: str | None = None,
              limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    """Recent runs, newest first.

    `day` narrows to one calendar day in `tz` (the viewer's IANA zone, UTC when
    unset) — the same buckets /stats/daily counts by, because a heatmap square
    and the list it opens have to contain the same runs.
    """
    stmt = select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(limit)
    if status: stmt = stmt.where(WorkflowRun.status == status)
    if workflow_id: stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
    if day:
        try:
            start, end = day_bounds(day, resolve_zone(tz))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        stmt = stmt.where(WorkflowRun.created_at >= start, WorkflowRun.created_at < end)
    return db.scalars(stmt).all()


@router.get("/runs/{object_id}", response_model=WorkflowRunDetail)
def get_run(object_id: int, db: Session = Depends(get_db)):
    obj = db.scalar(select(WorkflowRun).where(WorkflowRun.id == object_id).options(
        selectinload(WorkflowRun.task_runs).selectinload(TaskRun.task)))
    if not obj: raise HTTPException(404, "WorkflowRun not found")
    return obj


@router.post("/runs/{object_id}/cancel", response_model=WorkflowRunOut)
def cancel_run(object_id: int, db: Session = Depends(get_db)):
    """Cancel a queued run immediately; a running run stops before its next task.

    A run parked on an approval gate is cancellable too, and must be: approve
    and reject are its only other exits, so without this a gate nobody decides
    holds a concurrency slot forever.
    """
    run = get_or_404(db, WorkflowRun, object_id)
    if run.status not in (RunStatus.queued, RunStatus.running, RunStatus.waiting_approval):
        raise HTTPException(409, f"Run is already {run.status.value}")
    # A running run finalizes itself once its current task returns; the other two
    # have no worker attached, so this request is what ends them.
    settles_now = run.status in (RunStatus.queued, RunStatus.waiting_approval)
    run.status = RunStatus.cancelled
    if settles_now:
        run.finished_at = now()
    # The undecided rows are settled on every path, including a running run's:
    # its worker never revisits a gate another branch opened, so that row would
    # be left awaiting_approval on a terminal run — invisible to /approvals and
    # counted against every later segment's re-entry.
    for task_run in db.scalars(select(TaskRun).where(
            TaskRun.workflow_run_id == run.id,
            TaskRun.status.in_((TaskRunStatus.queued, TaskRunStatus.awaiting_approval)))):
        task_run.status = TaskRunStatus.cancelled
    db.commit()
    db.refresh(run)
    ws_manager.notify({"type": "run_updated", "id": run.id})
    return run


@router.post("/runs/{object_id}/retry", response_model=WorkflowRunOut, status_code=201)
def retry_run(object_id: int, db: Session = Depends(get_db)):
    """Queue a fresh run of the same workflow with this run's parameters.

    Allowed even when the workflow is disabled/auto-paused: 'enabled' only
    gates the scheduler, and retrying is how you verify a fix before unpausing.
    """
    source = get_or_404(db, WorkflowRun, object_id)
    if source.status in (RunStatus.queued, RunStatus.running, RunStatus.waiting_approval):
        raise HTTPException(409, "Run is still in progress")
    workflow = get_or_404(db, Workflow, source.workflow_id)
    # The same gate /run and /resume apply. Without it, retrying a workflow
    # whose tasks or environment have since gone produces a run that fails with
    # no task rows at all, and that phantom failure counts toward the
    # auto-pause streak — one click silently disabling the schedule.
    _ensure_workflow_runnable(db, workflow)
    run = create_run(db, workflow, TriggerType.manual, dict(source.parameters_json or {}))
    ws_manager.notify({"type": "run_created", "id": run.id, "workflow_id": run.workflow_id})
    return run


@router.get("/task-runs/{object_id}", response_model=TaskRunOut)
def get_task_run(object_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, TaskRun, object_id)


def log_response(task_run: TaskRun, attr: str, tail_bytes: int | None = None):
    path = getattr(task_run, attr)
    if not path: return PlainTextResponse("", status_code=200)
    file = Path(path)
    if not file.is_file(): raise HTTPException(404, "Log file not found")
    if tail_bytes is not None:
        # Serve only the end of the file so huge logs never sit in API memory.
        size = file.stat().st_size
        with file.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            data = handle.read()
        return PlainTextResponse(data.decode("utf-8", errors="replace"))
    return PlainTextResponse(file.read_text(errors="replace"))


@router.get("/task-runs/{object_id}/stdout")
def stdout(object_id: int, tail_bytes: int | None = Query(None, ge=1),
           db: Session = Depends(get_db)):
    return log_response(get_or_404(db, TaskRun, object_id), "stdout_log_path", tail_bytes)


@router.get("/task-runs/{object_id}/stderr")
def stderr(object_id: int, tail_bytes: int | None = Query(None, ge=1),
           db: Session = Depends(get_db)):
    return log_response(get_or_404(db, TaskRun, object_id), "stderr_log_path", tail_bytes)


@router.get("/stats/summary")
def stats_summary(db: Session = Depends(get_db)):
    """Dashboard headline metrics, aggregated in SQL.

    The UI previously derived these from a capped /runs fetch, which silently
    under-counts once a workflow runs often enough to overflow the limit (a
    5-minute schedule is ~288 runs/day). Aggregating in the database keeps the
    numbers correct at any volume.
    """
    # Aware UTC, never a naive cutoff: PostgreSQL reads a naive bind against
    # timestamptz in the session timezone, and SQLite drops the offset anyway.
    since_24h = now() - timedelta(days=1)
    since_7d = now() - timedelta(days=7)

    def count(*conditions) -> int:
        return db.scalar(select(func.count()).select_from(WorkflowRun).where(*conditions)) or 0

    running = count(WorkflowRun.status == RunStatus.running)
    queued = count(WorkflowRun.status == RunStatus.queued)
    # A run parked on an approval gate is live: it holds a concurrency slot and
    # is waiting on a person, so a dashboard that omitted it would show "nothing
    # live" while a pipeline sat stopped.
    waiting = count(WorkflowRun.status == RunStatus.waiting_approval)
    done_7d = count(WorkflowRun.created_at >= since_7d,
                    WorkflowRun.status.in_((RunStatus.success, RunStatus.failed)))
    success_7d = count(WorkflowRun.created_at >= since_7d, WorkflowRun.status == RunStatus.success)
    avg_24h = db.scalar(
        select(func.avg(WorkflowRun.duration_seconds)).where(
            WorkflowRun.created_at >= since_24h, WorkflowRun.duration_seconds.is_not(None)))
    return {
        "running": running,
        "queued": queued,
        "waiting": waiting,
        "live": running + queued + waiting,
        "runs_24h": count(WorkflowRun.created_at >= since_24h),
        "succeeded_24h": count(WorkflowRun.created_at >= since_24h,
                               WorkflowRun.status == RunStatus.success),
        "failed_24h": count(WorkflowRun.created_at >= since_24h,
                            WorkflowRun.status == RunStatus.failed),
        "avg_duration_24h": float(avg_24h) if avg_24h is not None else None,
        "done_7d": done_7d,
        "success_7d": success_7d,
        "success_rate_7d": round(success_7d / done_7d * 100) if done_7d else None,
    }


@router.get("/stats/daily")
def daily_stats(days: int = Query(112, ge=1, le=366), workflow_id: int | None = None,
                tz: str | None = None, db: Session = Depends(get_db)):
    """Per-day run counts by outcome, for activity heatmaps.

    `tz` is the viewer's IANA zone; days are that zone's calendar days, so a
    9pm run lands on the evening the operator remembers rather than on the next
    UTC date. Omitted means UTC, which is what the endpoint always did.
    """
    try:
        zone = resolve_zone(tz)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    until = now()
    since = until - timedelta(days=days)
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"

    buckets: dict[str, dict] = {}
    # One query per span of constant offset. A window without a DST transition
    # is a single query, exactly as before.
    for start, end, offset in offset_segments(since, until, zone):
        day = local_date_expr(WorkflowRun.created_at, offset, dialect)
        stmt = (select(day.label("day"), WorkflowRun.status, func.count())
                .where(WorkflowRun.created_at >= start, WorkflowRun.created_at < end)
                .group_by(day, WorkflowRun.status))
        if workflow_id:
            stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
        for value, status, count in db.execute(stmt):
            key = str(value)[:10]
            entry = buckets.setdefault(
                key, {"date": key, "success": 0, "failed": 0, "other": 0})
            name = getattr(status, "value", str(status))
            if name in ("success", "failed"):
                entry[name] += count
            else:
                entry["other"] += count
    return sorted(buckets.values(), key=lambda item: item["date"])


@router.get("/artifacts")
def list_artifacts(workflow_run_id: int | None = None, task_run_id: int | None = None,
                   limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    stmt = select(Artifact).order_by(Artifact.created_at.desc()).limit(limit)
    if workflow_run_id: stmt = stmt.where(Artifact.workflow_run_id == workflow_run_id)
    if task_run_id: stmt = stmt.where(Artifact.task_run_id == task_run_id)
    return db.scalars(stmt).all()


@router.get("/artifacts/{object_id}")
def get_artifact(object_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, Artifact, object_id)


@router.get("/artifacts/{object_id}/download")
def download_artifact(object_id: int, db: Session = Depends(get_db)):
    artifact = get_or_404(db, Artifact, object_id)
    path = Path(artifact.path)
    if not path.is_file(): raise HTTPException(404, "Artifact file not found")
    return FileResponse(path, filename=artifact.name)
