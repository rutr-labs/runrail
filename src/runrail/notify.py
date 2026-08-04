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

from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.config import get_settings
from runrail.models import RunStatus, Workflow, WorkflowRun

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
    url = workflow.notify_webhook_url or get_settings().notify_webhook_url

    if run.status == RunStatus.failed:
        if url and _previous_outcome(db, run) != RunStatus.failed:
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
