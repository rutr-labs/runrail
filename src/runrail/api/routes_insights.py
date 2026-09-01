"""Insights: log search across runs, run notes, task duration trends, and the
scheduled runs that never happened."""

from datetime import datetime
from statistics import median
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runrail.api.crud import apply_update, get_or_404, save
from runrail.api.ws import manager as ws_manager
from runrail.daybuckets import resolve_zone
from runrail.db import get_db
from runrail.logsearch import search_logs
from runrail.models import (
    RunNote,
    RunStatus,
    Task,
    TaskRun,
    TaskRunStatus,
    Workflow,
    WorkflowRun,
    _aware,
)
from runrail.schedule_gaps import (
    DEFAULT_DAYS,
    DEFAULT_FIRES,
    DEFAULT_MISSED,
    MAX_FIRES,
    find_gaps,
)
from runrail.schemas import RunNoteIn, RunNoteOut

router = APIRouter(prefix="/api", tags=["insights"])

# Every constant below is a knob someone will want to tune; the rationale for
# each is in _task_stats.
SLOW_MIN_SAMPLES = 5
SLOW_SIGMA = 3.0
SLOW_MIN_DELTA_SECONDS = 5.0
SLOW_MIN_DURATION_SECONDS = 10.0
_MAD_SCALE = 1.4826  # makes "sigma" mean what people expect for normal data
_SPREAD_FLOOR = 0.25  # of the median, for the MAD == 0 case
NOTE_PREVIEW_CHARS = 120


@router.get("/logs/search")
def log_search(
    q: str = Query(..., min_length=2), regex: bool = False, case_sensitive: bool = False,
    workflow_id: int | None = None, task_id: int | None = None, task_name: str | None = None,
    status: RunStatus | None = None, task_status: TaskRunStatus | None = None,
    stream: Literal["stdout", "stderr", "both"] = "both",
    since: datetime | None = None, until: datetime | None = None,
    limit: int = Query(50, ge=1, le=500), context: int = Query(2, ge=0, le=10),
    max_files: int = Query(2000, ge=1, le=20000),
    max_bytes_per_file: int = Query(5_000_000, ge=1024),
    timeout_ms: int = Query(5000, ge=100, le=30000),
    db: Session = Depends(get_db),
):
    """Search the newest logs in scope; see logsearch for the bounds.

    Deliberately a plain `def`: FastAPI runs it in the threadpool, so this
    blocking file I/O never stalls the event loop or the live log tail.
    """
    try:
        return search_logs(
            db, q=q, regex=regex, case_sensitive=case_sensitive, workflow_id=workflow_id,
            task_id=task_id, task_name=task_name, status=status, task_status=task_status,
            stream=stream, since=since, until=until, limit=limit, context=context,
            max_files=max_files, max_bytes_per_file=max_bytes_per_file, timeout_ms=timeout_ms)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/runs/notes/summary")
def notes_summary(workflow_id: int | None = None, limit: int = Query(500, ge=1, le=5000),
                  db: Session = Depends(get_db)):
    """Note counts keyed by run id, so a 500-row run table can flag annotated
    runs from one query instead of a subquery on every /runs response."""
    counts = (select(RunNote.workflow_run_id, func.count().label("total"))
              .group_by(RunNote.workflow_run_id)
              .order_by(RunNote.workflow_run_id.desc()).limit(limit))
    if workflow_id:
        counts = counts.join(WorkflowRun, RunNote.workflow_run_id == WorkflowRun.id).where(
            WorkflowRun.workflow_id == workflow_id)
    totals = dict(db.execute(counts).all())
    if not totals:
        return {}
    previews: dict[int, str] = {}
    for run_id, body in db.execute(
            select(RunNote.workflow_run_id, RunNote.body)
            .where(RunNote.workflow_run_id.in_(totals))
            .order_by(RunNote.created_at, RunNote.id)):
        previews.setdefault(run_id, body[:NOTE_PREVIEW_CHARS])
    return {str(run_id): {"count": total, "preview": previews.get(run_id, "")}
            for run_id, total in totals.items()}


@router.get("/runs/{run_id}/notes", response_model=list[RunNoteOut])
def list_notes(run_id: int, db: Session = Depends(get_db)):
    """Oldest first — a note thread reads as a timeline, not a feed."""
    get_or_404(db, WorkflowRun, run_id)
    return db.scalars(select(RunNote).where(RunNote.workflow_run_id == run_id)
                      .order_by(RunNote.created_at, RunNote.id)).all()


@router.post("/runs/{run_id}/notes", response_model=RunNoteOut, status_code=201)
def create_note(run_id: int, payload: RunNoteIn, db: Session = Depends(get_db)):
    get_or_404(db, WorkflowRun, run_id)
    note = save(db, RunNote(workflow_run_id=run_id, **payload.model_dump()))
    ws_manager.notify({"type": "run_notes_changed", "run_id": run_id})
    return note


@router.put("/run-notes/{note_id}", response_model=RunNoteOut)
def update_note(note_id: int, payload: RunNoteIn, db: Session = Depends(get_db)):
    note = save(db, apply_update(get_or_404(db, RunNote, note_id),
                            payload.model_dump(exclude_unset=True)))
    ws_manager.notify({"type": "run_notes_changed", "run_id": note.workflow_run_id})
    return note


@router.delete("/run-notes/{note_id}", status_code=204)
def delete_note(note_id: int, db: Session = Depends(get_db)):
    note = get_or_404(db, RunNote, note_id)
    run_id = note.workflow_run_id
    db.delete(note)
    db.commit()
    ws_manager.notify({"type": "run_notes_changed", "run_id": run_id})
    return Response(status_code=204)


def _percentile(values: list[float], fraction: float) -> float:
    position = fraction * (len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (position - low) * (values[high] - values[low])


def _task_stats(durations: list[float]) -> dict:
    """The robust "slower than usual" rule, in one place.

    Median plus a MAD-derived spread, never a mean and never a stdev: one
    pathological 40-minute historical run inflates a stdev enough to mask every
    later regression, while the median absolute deviation ignores it. A task is
    slow only when all three hold — enough history to have a "usual", far
    enough outside the spread, and far enough out in absolute seconds. That
    last clause is what stops the crying wolf: nobody cares that a task went
    from 0.3s to 1.1s, however many multiples that is.

    Mirrored client-side; `spread` is returned so the two cannot drift.
    """
    ordered = sorted(durations)
    middle = median(ordered)
    deviation = median([abs(value - middle) for value in ordered])
    # The floor handles MAD == 0: a task that always takes exactly 2.0s would
    # otherwise degenerate to "any increase at all is slow".
    spread = max(_MAD_SCALE * deviation, _SPREAD_FLOOR * middle)
    last = durations[-1]
    slow = (len(durations) >= SLOW_MIN_SAMPLES
            and last > middle + SLOW_SIGMA * spread
            and last - middle >= SLOW_MIN_DELTA_SECONDS
            and last >= SLOW_MIN_DURATION_SECONDS)
    return {
        "median": round(middle, 3), "p90": round(_percentile(ordered, 0.9), 3),
        "spread": round(spread, 3), "last": last, "slow": slow,
        "slow_ratio": round(last / middle, 2) if middle else None,
    }


@router.get("/workflows/{workflow_id}/task-durations")
def task_durations(workflow_id: int, window: int = Query(20, ge=5, le=100),
                   db: Session = Depends(get_db)):
    """Per-task duration history plus a slow flag, for sparklines.

    Baseline is SUCCESSFUL task runs only: a task that fails fast in 2s would
    otherwise drag the median down and make every healthy run look slow. Tasks
    with no successful history are simply absent — no data, no claim.
    """
    get_or_404(db, Workflow, workflow_id)
    # Per-task window, not a flat LIMIT: a rarely-run task inside a busy
    # workflow is exactly the one you most want a trend for. row_number() needs
    # SQLite >= 3.25, guaranteed under requires-python >= 3.11.
    ranked = (select(TaskRun.id.label("task_run_id"), TaskRun.task_id,
                     TaskRun.workflow_run_id, TaskRun.duration_seconds, TaskRun.created_at,
                     func.row_number().over(partition_by=TaskRun.task_id,
                                            order_by=TaskRun.id.desc()).label("rn"))
              .join(WorkflowRun, TaskRun.workflow_run_id == WorkflowRun.id)
              .where(WorkflowRun.workflow_id == workflow_id,
                     TaskRun.status == TaskRunStatus.success,
                     TaskRun.duration_seconds.is_not(None))
              .subquery())
    names = dict(db.execute(
        select(Task.id, Task.name).where(Task.workflow_id == workflow_id)).all())
    history: dict[int, list[dict]] = {}
    # Oldest-first, so the sparkline renders left to right with no client-side
    # reversal.
    for row in db.execute(select(ranked).where(ranked.c.rn <= window)
                          .order_by(ranked.c.task_id, ranked.c.task_run_id)):
        history.setdefault(row.task_id, []).append({
            "task_run_id": row.task_run_id, "workflow_run_id": row.workflow_run_id,
            "duration_seconds": row.duration_seconds, "created_at": _aware(row.created_at),
        })
    return [{"task_id": task_id, "task_name": names.get(task_id), "samples": samples,
             **_task_stats([s["duration_seconds"] for s in samples])}
            for task_id, samples in history.items()]


@router.get("/workflows/{workflow_id}/schedule-gaps")
def schedule_gaps(workflow_id: int, days: int = Query(DEFAULT_DAYS, ge=1, le=365),
                  max_fires: int = Query(DEFAULT_FIRES, ge=10, le=MAX_FIRES),
                  limit: int = Query(DEFAULT_MISSED, ge=1, le=MAX_FIRES),
                  tz: str | None = None, db: Session = Depends(get_db)):
    """The fires this workflow's schedule owed and what became of each one:
    `missed` for the run list, `daily` for the heatmap, `totals` for the header.

    Computed on read and never written — see schedule_gaps for why a placeholder
    run row would be a lie, and for the bound `stopped_by` reports.
    """
    try:
        zone = resolve_zone(tz)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return find_gaps(db, get_or_404(db, Workflow, workflow_id),
                     days=days, max_fires=max_fires, limit=limit, zone=zone)
