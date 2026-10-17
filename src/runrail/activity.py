"""The activity feed behind the notification bell: noteworthy events, derived.

Derived, never logged separately. Every event here is read from rows the app
already writes — and for the transitions, from the marks notify.py leaves at
the moment it decides (a run's `alert_kind`, a workflow's `auto_paused_at`),
so the feed and the webhooks cannot tell different stories. There is no events
table to migrate, retain or clean up.

A kind is named after the `event` field of the webhook it mirrors, a failure
surfaces on the *transition* rather than on every red run, and a snoozed
workflow contributes nothing.

Read state belongs to the client: it keeps a last-read instant in localStorage
(single user, one browser) and passes it back as `read_at`. Storing it here
would mean a column, a migration and a write on every poll of this endpoint.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runrail.models import (
    RunStatus,
    Task,
    TaskRun,
    TaskRunStatus,
    Workflow,
    WorkflowRun,
    _aware,
    now,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_WINDOW_HOURS = 24 * 7
MAX_WINDOW_HOURS = 24 * 30

#: Rows any single source may contribute. `total` is capped by it too — a badge
#: reading "200" is as actionable as one reading "4000", and this endpoint is
#: polled.
_SCAN_LIMIT = 200

#: Severity is the whole vocabulary the panel colours by; keep it to these four.
SEVERITY = {
    "run_failed": "error",
    "workflow_paused": "error",
    "sla_breached": "warning",
    "run_missed": "warning",
    "approval_requested": "info",
    "run_recovered": "success",
}


def _event(kind: str, key: int, at: datetime | None, workflow: Workflow, title: str,
           *, run_id: int | None = None, task_name: str | None = None) -> dict:
    # `id` is stable across polls so the client can key rows and diff them; the
    # key is whatever makes the event unique — a run, a gate, or the workflow.
    return {
        "id": f"{kind}:{key}", "kind": kind, "severity": SEVERITY[kind], "title": title,
        "at": _aware(at), "workflow_id": workflow.id, "workflow_name": workflow.name,
        "run_id": run_id, "task_name": task_name,
    }


def _outcome_events(db: Session, workflows: dict[int, Workflow], since: datetime) -> list[dict]:
    """The alerts notify.py decided, read back: a run carries `alert_kind` when
    its outcome moved the workflow's alert state. Reconstructing the transition
    from run ids instead disagreed with the webhooks whenever a run was resumed
    or two finished out of order."""
    rows = db.execute(
        select(WorkflowRun.id, WorkflowRun.workflow_id, WorkflowRun.created_at,
               WorkflowRun.finished_at, WorkflowRun.alert_kind)
        .where(WorkflowRun.alert_kind.is_not(None),
               func.coalesce(WorkflowRun.finished_at, WorkflowRun.created_at) >= since)
        .order_by(WorkflowRun.id.desc()).limit(_SCAN_LIMIT)).all()
    events = []
    for row in rows:
        workflow = workflows.get(row.workflow_id)
        if workflow is None:
            continue
        failed = row.alert_kind == "run_failed"
        events.append(_event(
            row.alert_kind, row.id, row.finished_at or row.created_at, workflow,
            f"{workflow.name} {'failed' if failed else 'recovered'} (run #{row.id})",
            run_id=row.id))
    return events


def _auto_pause_events(db: Session, workflows: dict[int, Workflow],
                       since: datetime) -> list[dict]:
    """A workflow disabled by its own consecutive-failure threshold: the pause
    stamps auto_paused_at and the run that tripped it, and switching the
    workflow back on clears them."""
    events = []
    for workflow in workflows.values():
        at = _aware(workflow.auto_paused_at)
        if at is None or at < since or workflow.enabled:
            continue
        events.append(_event(
            "workflow_paused", workflow.id, at, workflow,
            f"{workflow.name} paused after {workflow.auto_pause_failures} "
            f"consecutive failures", run_id=workflow.auto_paused_run_id))
    return events


def _approval_events(db: Session, workflows: dict[int, Workflow], since: datetime) -> list[dict]:
    """Gates open right now, defined exactly as /api/approvals defines them: a
    gate on a still-running run is actionable, one left on a terminal run is a
    decision nobody can make any more."""
    rows = db.execute(
        select(TaskRun.id, TaskRun.created_at, TaskRun.workflow_run_id,
               Task.name.label("task_name"), WorkflowRun.workflow_id)
        .join(Task, Task.id == TaskRun.task_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(TaskRun.status == TaskRunStatus.awaiting_approval,
               WorkflowRun.status.in_((RunStatus.running, RunStatus.waiting_approval)),
               TaskRun.created_at >= since)
        .order_by(TaskRun.id.desc()).limit(_SCAN_LIMIT)).all()
    events = []
    for row in rows:
        workflow = workflows.get(row.workflow_id)
        if workflow is None:
            continue
        events.append(_event(
            "approval_requested", row.id, row.created_at, workflow,
            f"{workflow.name} run #{row.workflow_run_id} is waiting for approval "
            f"on '{row.task_name}'", run_id=row.workflow_run_id, task_name=row.task_name))
    return events


def _sla_events(db: Session, workflows: dict[int, Workflow], since: datetime) -> list[dict]:
    """The breach marker is the event. The scheduler stamps it once per run and
    never stamps it at all while the workflow is snoozed, so there is nothing
    here to de-duplicate and nothing to mute a second time."""
    rows = db.execute(
        select(WorkflowRun.id, WorkflowRun.workflow_id, WorkflowRun.status,
               WorkflowRun.sla_breached_at)
        .where(WorkflowRun.sla_breached_at.is_not(None), WorkflowRun.sla_breached_at >= since)
        .order_by(WorkflowRun.sla_breached_at.desc()).limit(_SCAN_LIMIT)).all()
    events = []
    for row in rows:
        workflow = workflows.get(row.workflow_id)
        if workflow is None:
            continue
        events.append(_event(
            "sla_breached", row.id, row.sla_breached_at, workflow,
            f"Run #{row.id} of {workflow.name} passed its {workflow.sla_minutes}-minute "
            f"deadline and is still {row.status.value}", run_id=row.id))
    return events


def _missed_events(workflows: dict[int, Workflow], since: datetime) -> list[dict]:
    """No query of its own: missed_notified_at is the watchdog's single-writer
    marker, set on the alert and cleared the moment the schedule comes back, so
    its presence *is* "this workflow is currently silent"."""
    events = []
    for workflow in workflows.values():
        at = _aware(workflow.missed_notified_at)
        if at is None or at < since:
            continue
        events.append(_event("run_missed", workflow.id, at, workflow,
                             f"{workflow.name} has not run on schedule"))
    return events


def recent_events(db: Session, *, limit: int = DEFAULT_LIMIT,
                  window_hours: int = DEFAULT_WINDOW_HOURS,
                  read_at: datetime | None = None) -> dict:
    """The feed, newest first, plus the counts the bell badge needs.

    Five queries at most, whatever the workflow count — this is polled. The
    window applies to every kind uniformly, standing conditions included: a gate
    open for a fortnight or a schedule dead for a month has stopped being news
    and belongs to the dashboard that shows it permanently, not to the bell.
    """
    since = now() - timedelta(hours=window_hours)
    # Snoozed workflows drop out here, once, for every kind — the same single
    # gate notify.webhook_url() applies. Snooze expires by the clock alone, so
    # this mutes rather than deletes: the events reappear when the mute lifts,
    # which is the point of muting instead of dismissing.
    workflows = {workflow.id: workflow for workflow in db.scalars(select(Workflow))
                 if not workflow.snoozed}
    events: list[dict] = []
    if workflows:
        events += _outcome_events(db, workflows, since)
        events += _auto_pause_events(db, workflows, since)
        events += _approval_events(db, workflows, since)
        events += _sla_events(db, workflows, since)
        events += _missed_events(workflows, since)
    events.sort(key=lambda event: (event["at"], event["id"]), reverse=True)
    read = _aware(read_at)
    unread = len(events) if read is None else sum(1 for e in events if e["at"] > read)
    return {"events": events[:limit], "total": len(events), "unread": unread,
            "window_hours": window_hours, "generated_at": now()}
