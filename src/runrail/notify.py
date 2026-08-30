"""Run-outcome notifications: webhook posts on failure, recovery, and auto-pause.

Notifications fire on the failure *transition* (the first failed run after a
success), not on every failure — a 2-minute schedule that breaks overnight
should produce one alert, not three hundred. A recovery message closes the loop.

Payload shape follows the receiver. Slack and generic receivers get a flat
JSON object whose "text" field renders natively, with structured fields riding
along. Microsoft Teams retired the Office 365 connector webhooks (which also
rendered "text") in favor of Power Automate's "when a Teams webhook request is
received" flow — its standard template only posts Adaptive Cards it finds in
the "attachments" array — so Teams-shaped URLs get that envelope instead.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.config import get_settings
from runrail.models import RunStatus, TaskRun, Workflow, WorkflowRun, _aware, now

_TIMEOUT_SECONDS = 10

#: Hosts that mean "this is Teams": Power Automate flow triggers
#: (*.logic.azure.com, *.powerplatform.com) and the legacy O365 connector
#: domain (*.webhook.office.com) — retired, but cards render there too.
_TEAMS_HOST_SUFFIXES = (".logic.azure.com", ".powerplatform.com", ".webhook.office.com")


def _is_teams_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host.endswith(_TEAMS_HOST_SUFFIXES)


def _payload_for(url: str, text: str, fields: dict) -> dict:
    if not _is_teams_url(url):
        return {"text": text, **fields}
    # Adaptive Card 1.4 in the "type: message" envelope the default Power
    # Automate template iterates over; facts carry the structured fields.
    facts = [{"title": key.replace("_", " ").title(), "value": str(value)}
             for key, value in fields.items()]
    card: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": [
            {"type": "TextBlock", "text": text, "wrap": True, "weight": "Bolder"},
            *([{"type": "FactSet", "facts": facts}] if facts else []),
        ],
    }
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }],
    }


def _post(url: str, text: str, **fields) -> None:
    payload = _payload_for(url, text, fields)
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS).close()
    except Exception:
        pass  # a broken webhook must never break run finalization


def webhook_url(workflow: Workflow | None) -> str | None:
    """The receiver for this workflow's alerts, or None while it is snoozed.

    Every alert source resolves its URL through here rather than reading the
    field: snooze has to mute failures, approval requests and watchdogs alike,
    and a source that reads notify_webhook_url directly would keep talking.
    """
    if workflow is None or workflow.snoozed:
        return None
    return workflow.notify_webhook_url or get_settings().notify_webhook_url


def notify_approval_requested(db: Session, task_run: TaskRun) -> None:
    """One post per gate opening — every gate is a distinct human ask, so this
    is not transition-gated the way run outcomes are."""
    run = db.get(WorkflowRun, task_run.workflow_run_id)
    workflow = db.get(Workflow, run.workflow_id) if run else None
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"⏸️ RunRail: '{workflow.name}' run #{run.id} is waiting for approval "
               f"on task '{task_run.task_name}'.",
          event="approval_requested", workflow=workflow.name, run_id=run.id,
          task=task_run.task_name)


def notify_approval_rejected(db: Session, task_run: TaskRun) -> None:
    run = db.get(WorkflowRun, task_run.workflow_run_id)
    workflow = db.get(Workflow, run.workflow_id) if run else None
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"🚫 RunRail: '{workflow.name}' run #{run.id} was rejected by "
               f"{task_run.approved_by or 'someone'} at task '{task_run.task_name}'.",
          event="approval_rejected", workflow=workflow.name, run_id=run.id,
          task=task_run.task_name, approved_by=task_run.approved_by or "")


def notify_missed_run(workflow: Workflow, expected: datetime, last_run_at: str) -> None:
    """The dead man's switch fired. Callers own the missed_notified_at
    transition; this only shapes the message."""
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"⏰ RunRail: workflow '{workflow.name}' has not run since {last_run_at} "
               f"(expected {expected:%Y-%m-%d %H:%M} UTC)."
               + ("" if workflow.enabled else " The workflow is paused."),
          event="run_missed", workflow=workflow.name,
          expected_at=expected.isoformat(), last_run_at=last_run_at)


def notify_missed_run_recovered(workflow: Workflow) -> None:
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"✅ RunRail: workflow '{workflow.name}' is running on schedule again.",
          event="run_missed_recovered", workflow=workflow.name)


def notify_sla_breach(workflow: Workflow, run: WorkflowRun, deadline: datetime) -> None:
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"⏳ RunRail: run #{run.id} of '{workflow.name}' has passed its "
               f"{workflow.sla_minutes}-minute deadline and is still {run.status.value}.",
          event="sla_breached", workflow=workflow.name, run_id=run.id,
          deadline_at=deadline.isoformat(), status=run.status.value)


def _previous_outcome(db: Session, run: WorkflowRun) -> RunStatus | None:
    """The workflow's newest completed outcome before this run (success/failed only)."""
    return db.scalar(
        select(WorkflowRun.status)
        .where(
            WorkflowRun.workflow_id == run.workflow_id,
            WorkflowRun.id != run.id,
            WorkflowRun.status.in_((RunStatus.success, RunStatus.failed)),
        )
        .order_by(WorkflowRun.id.desc())
        .limit(1)
    )


def _consecutive_failures(db: Session, workflow_id: int, limit: int) -> int:
    """Trailing failed runs (cancelled runs are neutral and ignored)."""
    statuses = db.scalars(
        select(WorkflowRun.status)
        .where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status.in_((RunStatus.success, RunStatus.failed)),
        )
        .order_by(WorkflowRun.id.desc())
        .limit(limit)
    ).all()
    count = 0
    for status in statuses:
        if status != RunStatus.failed:
            break
        count += 1
    return count


def notify_run_outcome(db: Session, run: WorkflowRun) -> None:
    """Called after a run reaches success/failed. Sends transition alerts and
    enforces the workflow's auto-pause threshold."""
    workflow = db.get(Workflow, run.workflow_id)
    if workflow is None:
        return
    url = webhook_url(workflow)

    if run.status == RunStatus.failed:
        # resume_count > 0 means this run already alerted once. _previous_outcome
        # ignores the run itself, so it would find the older success and read a
        # re-failure as a fresh transition — one logical failure, two alerts.
        if url and not run.resume_count and _previous_outcome(db, run) != RunStatus.failed:
            _post(url, f"❌ RunRail: workflow '{workflow.name}' failed (run #{run.id}).",
                  event="run_failed", workflow=workflow.name, run_id=run.id)
        threshold = workflow.auto_pause_failures
        if (threshold and workflow.enabled
                and _consecutive_failures(db, workflow.id, threshold) >= threshold):
            workflow.enabled = False
            db.commit()
            if url:
                _post(url, f"⏸️ RunRail: workflow '{workflow.name}' paused after "
                           f"{threshold} consecutive failures. Re-enable it once fixed.",
                      event="workflow_paused", workflow=workflow.name, run_id=run.id)
    elif run.status == RunStatus.success and url:
        if _previous_outcome(db, run) == RunStatus.failed:
            _post(url, f"✅ RunRail: workflow '{workflow.name}' recovered (run #{run.id}).",
                  event="run_recovered", workflow=workflow.name, run_id=run.id)
        # Success only: a run that breached and then failed already sends
        # run_failed, and "it failed AND was late" is not separately actionable.
        if run.sla_breached_at is not None and workflow.sla_minutes:
            # Measured from created_at, matching the deadline itself. duration_seconds
            # starts at claim time, so using it would subtract the queue wait — and
            # queue wait is exactly what a creation-relative SLA exists to catch,
            # which produced a negative "finished -1 min past its deadline".
            started = _aware(run.created_at)
            finished = _aware(run.finished_at) or now()
            over = (finished - started).total_seconds() / 60 - workflow.sla_minutes
            _post(url, f"⌛ RunRail: '{workflow.name}' finished {over:.0f} min past its "
                       f"deadline (run #{run.id}).",
                  event="sla_finished_late", workflow=workflow.name, run_id=run.id)
