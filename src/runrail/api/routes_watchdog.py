"""Snooze: mute a workflow's alerts, and optionally its runs, until an instant."""

from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from runrail.api.crud import get_or_404, save
from runrail.db import get_db
from runrail.models import Workflow, now
from runrail.schemas import SnoozeIn, WorkflowOut

router = APIRouter(prefix="/api", tags=["watchdog"])

#: The SnoozeIn bound, expressed in minutes for the duration shorthand.
_MAX_SNOOZE_MINUTES = 30 * 24 * 60


@router.post("/workflows/{workflow_id}/snooze", response_model=WorkflowOut)
def snooze_workflow(
    workflow_id: int,
    data: SnoozeIn | None = None,
    minutes: int | None = Query(default=None, ge=1, le=_MAX_SNOOZE_MINUTES),
    pause_runs: bool = False,
    db: Session = Depends(get_db),
):
    """Mute every alert source for this workflow until an instant.

    An action, not configuration: snooze is deliberately absent from WorkflowIn
    because apply_update writes every key of the edit modal's body and would
    silently clear it. It expires by the clock alone — nothing to re-enable.
    """
    if minutes is not None:
        # Duration shorthand for curl and external timers; the UI sends an
        # absolute instant, since "tomorrow 9am" is a viewer-local concept the
        # browser knows and the server does not. Both paths land in SnoozeIn, so
        # the future/30-day bounds are enforced in exactly one place.
        data = SnoozeIn(until=now() + timedelta(minutes=minutes), pause_runs=pause_runs)
    if data is None:
        raise HTTPException(422, "Snooze needs an `until` instant or a `minutes` duration")
    workflow = get_or_404(db, Workflow, workflow_id)
    # SQLite stores a datetime's wall-clock components and drops the offset, so a
    # "+04:00" instant from the browser has to be converted here or it lands four
    # hours wrong (PostgreSQL would convert it and the two backends disagree).
    workflow.snooze_until = data.until.astimezone(timezone.utc)
    workflow.snooze_pauses_runs = data.pause_runs
    return save(db, workflow)


@router.delete("/workflows/{workflow_id}/snooze", response_model=WorkflowOut)
def unsnooze_workflow(workflow_id: int, db: Session = Depends(get_db)):
    workflow = get_or_404(db, Workflow, workflow_id)
    # missed_notified_at is left alone: the watchdog owns that transition, and a
    # workflow still silent when the mute lifts should alert, not stay quiet.
    workflow.snooze_until = None
    workflow.snooze_pauses_runs = False
    return save(db, workflow)
