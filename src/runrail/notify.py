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
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from runrail import runtime_settings
from runrail.models import RunStatus, TaskRun, Workflow, WorkflowRun, _aware, now

_TIMEOUT_SECONDS = 10
log = logging.getLogger("runrail.notify")

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


def deliver(url: str, text: str, **fields) -> str | None:
    """POST one message now; return None on success or the error as text."""
    payload = _payload_for(url, text, fields)
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS).close()
        return None
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _post(url: str, text: str, *, workflow_id: int | None = None, **fields) -> None:
    """Send a message without holding anyone up.

    Delivery runs on its own thread: it used to run on the worker thread that
    had just finished a run, inside the HTTP request that rejected a gate, and
    serially inside the sixty-second watchdog tick, so a receiver that did not
    answer held a worker slot — and with a few alerting workflows, skipped
    watchdog ticks. A failure is logged and, when the message belongs to a
    workflow, recorded on it; it used to vanish without trace, so a revoked
    Slack webhook simply never spoke again.
    """
    if not runtime_settings.alerts_allowed(fields.get("event")):
        return  # switched off in Settings, or quiet hours; Activity still has it
    threading.Thread(target=_send_and_record, args=(url, text, workflow_id, fields),
                     name="runrail-webhook", daemon=True).start()


def _send_and_record(url: str, text: str, workflow_id: int | None, fields: dict) -> None:
    error = deliver(url, text, **fields)
    if workflow_id is None:
        return
    try:
        from runrail.db import SessionLocal
        with SessionLocal() as db:
            db.execute(update(Workflow).where(Workflow.id == workflow_id).values(
                webhook_error=error, webhook_error_at=now() if error else None,
                updated_at=Workflow.updated_at))
            db.commit()
    except Exception:
        log.exception("could not record webhook outcome for workflow %s", workflow_id)
    if error:
        log.warning("webhook for workflow %s failed: %s", workflow_id, error)


def webhook_url(workflow: Workflow | None) -> str | None:
    """The receiver for this workflow's alerts, or None while it is snoozed.

    Every alert source resolves its URL through here rather than reading the
    field: snooze has to mute failures, approval requests and watchdogs alike,
    and a source that reads notify_webhook_url directly would keep talking.
    """
    if workflow is None or workflow.snoozed:
        return None
    return workflow.notify_webhook_url or runtime_settings.current("notify_webhook_url")


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
          task=task_run.task_name, workflow_id=workflow.id)


def notify_approval_rejected(db: Session, task_run: TaskRun) -> None:
    run = db.get(WorkflowRun, task_run.workflow_run_id)
    workflow = db.get(Workflow, run.workflow_id) if run else None
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"🚫 RunRail: '{workflow.name}' run #{run.id} was rejected at "
               f"task '{task_run.task_name}'.",
          event="approval_rejected", workflow=workflow.name, run_id=run.id,
          task=task_run.task_name, workflow_id=workflow.id)


def sla_anchor(run: WorkflowRun) -> datetime:
    """Where a run's SLA clock starts: its creation, or its latest resume.
    created_at never changes on resume, so yesterday's run resumed today was
    twenty-three hours late the moment it started."""
    return max(_aware(run.created_at), _aware(run.resumed_at) or _aware(run.created_at))


def notify_missed_run(workflow: Workflow, expected: datetime, last_run_at: str) -> None:
    """The dead man's switch fired. Callers own the missed_notified_at
    transition; this only shapes the message."""
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"⏰ RunRail: workflow '{workflow.name}' has not run since {last_run_at} "
               f"(expected {expected:%Y-%m-%d %H:%M} UTC)."
               + ("" if workflow.enabled else " The workflow is paused."),
          event="run_missed", workflow=workflow.name, workflow_id=workflow.id,
          expected_at=expected.isoformat(), last_run_at=last_run_at)


def notify_missed_run_recovered(workflow: Workflow) -> None:
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"✅ RunRail: workflow '{workflow.name}' is running on schedule again.",
          event="run_missed_recovered", workflow=workflow.name, workflow_id=workflow.id)


def notify_sla_breach(workflow: Workflow, run: WorkflowRun, deadline: datetime) -> None:
    url = webhook_url(workflow)
    if not url:
        return
    _post(url, f"⏳ RunRail: run #{run.id} of '{workflow.name}' has passed its "
               f"{workflow.sla_minutes}-minute deadline and is still {run.status.value}.",
          event="sla_breached", workflow=workflow.name, run_id=run.id,
          deadline_at=deadline.isoformat(), status=run.status.value, workflow_id=workflow.id)


def _consecutive_failures(db: Session, workflow: Workflow, limit: int) -> int:
    """Trailing failed runs since the workflow was last switched on (cancelled
    runs are neutral and ignored)."""
    stmt = (select(WorkflowRun.status)
            .where(WorkflowRun.workflow_id == workflow.id,
                   WorkflowRun.status.in_((RunStatus.success, RunStatus.failed)))
            .order_by(WorkflowRun.id.desc()).limit(limit))
    if workflow.enabled_at is not None:
        # Runs from before the last re-enable belong to the streak that paused
        # it then; counting them paused a just-fixed workflow after one failure.
        stmt = stmt.where(WorkflowRun.created_at >= workflow.enabled_at)
    count = 0
    for status in db.scalars(stmt):
        if status != RunStatus.failed:
            break
        count += 1
    return count


def _transition(db: Session, run: WorkflowRun) -> str | None:
    """Move the workflow's alert state for this outcome; the alert this run
    sends, or None. One UPDATE guarded on the state it expects, so of two runs
    finishing together only one alerts, and a resumed run that succeeds after
    failing moves the state back and recovers like any other."""
    if run.status == RunStatus.failed:
        kind, guard = "run_failed", or_(Workflow.alert_state.is_(None),
                                        Workflow.alert_state != "failing")
        state = "failing"
    else:
        kind, guard, state = "run_recovered", Workflow.alert_state == "failing", "ok"
    won = db.execute(update(Workflow).where(Workflow.id == run.workflow_id, guard)
                     .values(alert_state=state, alert_run_id=run.id,
                             updated_at=Workflow.updated_at)).rowcount == 1
    if not won and run.status == RunStatus.success:
        # A first-ever success says nothing; it only sets the state.
        db.execute(update(Workflow).where(Workflow.id == run.workflow_id,
                                          Workflow.alert_state.is_(None))
                   .values(alert_state="ok", alert_run_id=run.id, updated_at=Workflow.updated_at))
    if won:
        db.execute(update(WorkflowRun).where(WorkflowRun.id == run.id).values(alert_kind=kind))
    db.commit()
    return kind if won else None


def notify_run_outcome(db: Session, run: WorkflowRun) -> None:
    """Called after a run reaches success/failed. Sends transition alerts and
    enforces the workflow's auto-pause threshold."""
    workflow = db.get(Workflow, run.workflow_id)
    if workflow is None or run.status not in (RunStatus.success, RunStatus.failed):
        return
    url = webhook_url(workflow)
    alert = _transition(db, run)

    if run.status == RunStatus.failed:
        # The pause is decided and committed before anything is posted: with
        # the post first, the queued iteration could be claimed during it and
        # run once more after "paused".
        threshold = workflow.auto_pause_failures
        paused = bool(threshold) and _consecutive_failures(db, workflow, threshold) >= threshold \
            and db.execute(update(Workflow).where(Workflow.id == workflow.id, Workflow.enabled.is_(True))
                           .values(enabled=False, auto_paused_at=now(), auto_paused_run_id=run.id,
                                   updated_at=Workflow.updated_at)).rowcount == 1
        db.commit()
        if url and alert:
            _post(url, f"❌ RunRail: workflow '{workflow.name}' failed (run #{run.id}).",
                  event="run_failed", workflow=workflow.name, run_id=run.id,
                  workflow_id=workflow.id)
        if url and paused:
            _post(url, f"⏸️ RunRail: workflow '{workflow.name}' paused after "
                       f"{threshold} consecutive failures. Re-enable it once fixed.",
                  event="workflow_paused", workflow=workflow.name, run_id=run.id,
                  workflow_id=workflow.id)
    elif url:
        if alert:
            _post(url, f"✅ RunRail: workflow '{workflow.name}' recovered (run #{run.id}).",
                  event="run_recovered", workflow=workflow.name, run_id=run.id,
                  workflow_id=workflow.id)
        # Success only: a run that breached and then failed already sends
        # run_failed, and "it failed AND was late" is not separately actionable.
        if run.sla_breached_at is not None and workflow.sla_minutes:
            # Measured from created_at, matching the deadline itself. duration_seconds
            # starts at claim time, so using it would subtract the queue wait — and
            # queue wait is exactly what a creation-relative SLA exists to catch,
            # which produced a negative "finished -1 min past its deadline".
            started = sla_anchor(run)
            finished = _aware(run.finished_at) or now()
            over = (finished - started).total_seconds() / 60 - workflow.sla_minutes
            _post(url, f"⌛ RunRail: '{workflow.name}' finished {over:.0f} min past its "
                       f"deadline (run #{run.id}).",
                  event="sla_finished_late", workflow=workflow.name, run_id=run.id,
                  workflow_id=workflow.id)
