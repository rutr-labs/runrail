"""Run-outcome notifications: webhook posts on failure, recovery, and auto-pause.

Notifications fire on the failure *transition* (the first failed run after a
success), not on every failure — a 2-minute schedule that breaks overnight
should produce one alert, not three hundred. A recovery message closes the loop.
The payload's "text" field renders natively in Slack and Microsoft Teams
incoming webhooks; structured fields ride along for generic consumers.
"""

import json
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.config import get_settings
from runrail.models import RunStatus, Workflow, WorkflowRun

_TIMEOUT_SECONDS = 10


def _post(url: str, text: str, **fields) -> None:
    payload = {"text": text, **fields}
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
