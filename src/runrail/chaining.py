"""Workflows that start after other workflows.

A workflow starts in one of three ways: on its schedule, after other
workflows, or only when someone runs it. Never on a schedule and after others
at once: "does it run twice, does the schedule wait?" has no answer anyone
can read off the page.

"After" means one thing: **once every workflow it runs after has succeeded
since it last started, it starts.** With one upstream that is "after each
success". With several it waits for all of them, so it never runs on inputs
only half refreshed; upstreams on different rhythms (every 15 minutes, daily)
make it run at the slower one.

There are no dates in this on purpose. Pipelines that run every 15 minutes or
every hour keep their own place in tracking tables ("process what is new since
the last success"); what they need from RunRail is to start at the right
moment, not to be told what to work on. So the rules are the schedule's:

- One run waits, never a pile. While a run of the downstream is queued, more
  upstream successes add nothing: when it starts, it picks up everything new.
- Any success counts: scheduled, by hand, resumed, backfilled.
- A failure starts nothing. The upstream's own alert covers it, and the
  downstream's page says what it is waiting for.

The check happens twice: right after a run's success is recorded (so the
downstream starts at once), and on the scheduler's minute tick (so a process
that stopped between the two still starts it). Both are idempotent: the run
key names the downstream's latest run, so two checks racing on the same state
insert one run.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runrail.models import (
    RunStatus,
    Task,
    TriggerType,
    Workflow,
    WorkflowRun,
    WorkflowUpstream,
    _aware,
)
from runrail.run_parameters import local_today, resolve


class ChainError(ValueError):
    """A "runs after" list the API refuses, with the reason in words."""


# ─── Configuration ─────────────────────────────────────────


def check_upstreams(db: Session, workflow_id: int | None, name: str,
                    upstream_ids: list[int], schedule_cron: str | None) -> list[int]:
    """The list as it will be stored (duplicates dropped, order kept), or a
    ChainError: a schedule as well, itself, a workflow that does not exist, or
    a loop."""
    ids = list(dict.fromkeys(upstream_ids))
    if not ids:
        return []
    if schedule_cron:
        raise ChainError("A workflow starts on a schedule or after other workflows, not both. "
                         "Clear the schedule to run it after others.")
    if workflow_id is not None and workflow_id in ids:
        raise ChainError("A workflow cannot run after itself")
    names = dict(db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(ids))).all())
    missing = [str(i) for i in ids if i not in names]
    if missing:
        raise ChainError(f"No workflow with id {', '.join(missing)}")
    if workflow_id is not None:
        # Follow "runs after" back from each upstream. Reaching this workflow
        # means it already runs (perhaps indirectly) before that upstream.
        graph: dict[int, list[int]] = {}
        for down, up in db.execute(select(WorkflowUpstream.workflow_id, WorkflowUpstream.upstream_id)):
            if down != workflow_id:
                graph.setdefault(down, []).append(up)
        for start in ids:
            seen: set[int] = set()
            stack = [start]
            while stack:
                node = stack.pop()
                if node == workflow_id:
                    raise ChainError(f"That makes a loop: '{names[start]}' already runs after "
                                     f"'{name}', directly or through others")
                if node not in seen:
                    seen.add(node)
                    stack.extend(graph.get(node, ()))
    return ids


def set_upstreams(workflow: Workflow, ids: list[int]) -> None:
    """Replace the links, keeping the ones that stay. A kept link keeps its
    created_at, so editing a workflow's description does not reset what its
    upstreams have already done."""
    kept = {link.upstream_id: link for link in workflow.upstream_links}
    workflow.upstream_links = [kept.get(i) or WorkflowUpstream(upstream_id=i) for i in ids]


def downstream_ids(db: Session, upstream_id: int) -> list[int]:
    return list(db.scalars(select(WorkflowUpstream.workflow_id)
                           .where(WorkflowUpstream.upstream_id == upstream_id)
                           .order_by(WorkflowUpstream.workflow_id)))


# ─── When a chained workflow is due ────────────────────────


def _latest_run(db: Session, workflow_id: int) -> WorkflowRun | None:
    return db.scalar(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
                     .order_by(WorkflowRun.id.desc()).limit(1))


def _last_start(run: WorkflowRun | None) -> datetime | None:
    """When the workflow last started: its newest run's latest beginning (a
    resume is a start too)."""
    if run is None:
        return None
    moments = [_aware(t) for t in (run.created_at, run.started_at, run.resumed_at) if t]
    return max(moments) if moments else None


def _anchor(workflow: Workflow, link: WorkflowUpstream, last_start: datetime | None) -> datetime:
    """The moment after which an upstream's success counts for this link:
    the downstream's last start, the link being made, or the downstream being
    switched back on, whichever is latest."""
    moments = [m for m in (last_start, _aware(link.created_at), _aware(workflow.enabled_at)) if m]
    return max(moments)


def _success_since(db: Session, upstream_id: int, anchor: datetime) -> WorkflowRun | None:
    return db.scalar(select(WorkflowRun).where(
        WorkflowRun.workflow_id == upstream_id,
        WorkflowRun.status == RunStatus.success,
        WorkflowRun.finished_at > anchor,
    ).order_by(WorkflowRun.finished_at.desc()).limit(1))


def _has_queued_run(db: Session, workflow_id: int) -> bool:
    return bool(db.scalar(select(func.count()).select_from(WorkflowRun).where(
        WorkflowRun.workflow_id == workflow_id, WorkflowRun.status == RunStatus.queued)))


def start_if_due(db: Session, workflow: Workflow) -> WorkflowRun | None:
    """Queue a run of a chained workflow if every upstream has succeeded since
    it last started; the run, or None when it is not due (or another check
    queued it first)."""
    links = list(workflow.upstream_links)
    if not links or workflow.schedule_cron:
        return None
    # Switched off, or snoozed with "pause runs": the same gates a schedule has.
    if not workflow.enabled or (workflow.snoozed and workflow.snooze_pauses_runs):
        return None
    if not db.scalar(select(func.count()).select_from(Task).where(Task.workflow_id == workflow.id)):
        return None  # a run with nothing in it would only fail
    if _has_queued_run(db, workflow.id):
        return None  # one waiting is enough; it will pick up everything new
    latest = _latest_run(db, workflow.id)
    last_start = _last_start(latest)
    successes = []
    for link in links:
        success = _success_since(db, link.upstream_id, _anchor(workflow, link, last_start))
        if success is None:
            return None
        successes.append(success)
    cause = max(successes, key=lambda r: _aware(r.finished_at))
    values = resolve(workflow.run_parameters_json, None,
                     day=local_today(workflow.schedule_timezone), strict=False)
    run = WorkflowRun(
        workflow_id=workflow.id, status=RunStatus.queued, trigger_type=TriggerType.upstream,
        # Names the run this one follows: two checks of the same state collide
        # here, and the second inserts nothing.
        run_key=f"after:{workflow.id}:{latest.id if latest else 0}",
        parameters_json=values or None, triggered_by_run_id=cause.id,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    from runrail.api.ws import manager
    manager.notify({"type": "run_created", "id": run.id, "workflow_id": workflow.id})
    return run


def after_success(db: Session, run: WorkflowRun) -> list[WorkflowRun]:
    """Called once a run has landed success: start whatever runs after its
    workflow and is now due."""
    started = []
    for workflow_id in downstream_ids(db, run.workflow_id):
        workflow = db.get(Workflow, workflow_id)
        if workflow is None:
            continue
        db.refresh(workflow)
        queued = start_if_due(db, workflow)
        if queued is not None:
            started.append(queued)
    return started


def sweep(db: Session) -> list[WorkflowRun]:
    """The minute check: start every chained workflow that is due. Catches a
    success whose immediate check never ran (the process stopped in between),
    and a downstream switched back on after its upstreams moved."""
    started = []
    chained = db.scalars(select(Workflow).where(
        Workflow.id.in_(select(WorkflowUpstream.workflow_id).distinct()))).all()
    for workflow in chained:
        queued = start_if_due(db, workflow)
        if queued is not None:
            started.append(queued)
    return started


# ─── What the page shows ───────────────────────────────────


def chain_view(db: Session, workflow: Workflow) -> dict[str, Any]:
    """Where a workflow sits in its chain: each upstream with its last result
    and whether it has succeeded since this workflow last started, and what
    runs after this one."""
    last_start = _last_start(_latest_run(db, workflow.id))
    after = []
    for link in workflow.upstream_links:
        upstream = db.get(Workflow, link.upstream_id)
        if upstream is None:
            continue
        last = db.scalar(select(WorkflowRun).where(
            WorkflowRun.workflow_id == upstream.id,
            WorkflowRun.status.in_((RunStatus.success, RunStatus.failed, RunStatus.running)),
        ).order_by(WorkflowRun.id.desc()).limit(1))
        after.append({
            "id": upstream.id, "name": upstream.name, "enabled": upstream.enabled,
            "last_run_id": last.id if last else None,
            "last_status": last.status.value if last else None,
            "last_finished_at": _aware(last.finished_at) if last else None,
            "done": _success_since(db, upstream.id, _anchor(workflow, link, last_start)) is not None,
        })
    then = [{"id": w.id, "name": w.name} for w in db.scalars(
        select(Workflow).where(Workflow.id.in_(downstream_ids(db, workflow.id))).order_by(Workflow.name))]
    return {"after": after, "then": then, "queued": _has_queued_run(db, workflow.id)}
