from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runrail import variables
from runrail.models import Environment, EnvironmentStatus, TriggerType, Workflow, WorkflowRun
from runrail.run_parameters import ParameterError, local_today, needs_value, resolve


def save(db: Session, obj):
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "A record with that name already exists") from exc


def get_or_404(db: Session, model, object_id: int):
    obj = db.get(model, object_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


def apply_update(obj, data: dict[str, Any]):
    """Write the given keys onto the row, and only those keys.

    Callers must pass ``model_dump(exclude_unset=True)``. A full dump carries
    pydantic's DEFAULTS for every field the client left out, so a form that
    forgets one field silently resets it — that is how an edit in the task
    modal used to strip a task's approval gate, turning a task that waits for a
    human into one that runs unattended. Omitted means unchanged.
    """
    for key, value in data.items():
        setattr(obj, key, value)
    return obj


def ensure_environment_ready(db: Session, environment_id: int | None) -> None:
    if environment_id is None:
        return
    environment = get_or_404(db, Environment, environment_id)
    if environment.status not in (EnvironmentStatus.ready, EnvironmentStatus.degraded):
        raise HTTPException(
            409,
            f"Environment '{environment.name}' is not ready: "
            f"{environment.last_error or environment.status.value}",
        )


def _no_secrets(values: dict) -> dict:
    """A run's parameters are stored and shown as typed, so a secret's value
    is refused there: it would sit in the clear on the run page."""
    masker = variables.masker()
    for name, value in values.items():
        if masker and isinstance(value, str) and masker.text(value) != value:
            raise ParameterError(f"{name}: that is the value of a saved secret. Parameters are shown "
                                 "as typed; secrets reach tasks as environment variables instead")
    return values


def create_run(db: Session, workflow: Workflow, trigger: TriggerType, params=None, run_key=None):
    """Queue a run with its parameters checked against the workflow's run
    form and its defaults filled in. ParameterError says what is wrong."""
    values = _no_secrets(resolve(workflow.run_parameters_json, params,
                                 day=local_today(workflow.schedule_timezone)))
    return save(db, WorkflowRun(
        workflow_id=workflow.id, status="queued", trigger_type=trigger,
        parameters_json=values, run_key=run_key,
    ))


def check_schedulable(workflow: Workflow) -> None:
    """A scheduled run has nobody to ask for a value, so every required
    parameter of a scheduled workflow needs a default."""
    missing = needs_value(workflow.run_parameters_json) if workflow.schedule_cron else []
    if missing:
        raise ParameterError(f"Scheduled runs start without anyone to ask: give {', '.join(missing)} "
                             "a default, or make it optional")


#: The longest range one backfill may cover. Nothing in the queue copes well
#: with ten thousand rows arriving at once, and a typo in a year is the usual
#: way to ask for them.
MAX_BACKFILL_DAYS = 366


def create_backfill(db: Session, workflow: Workflow, start: date, end: date, params=None):
    """Queue one run per date in [start, end], skipping dates already backfilled.
    Raises ValueError for a range it will not queue."""
    if end < start:
        raise ValueError("Backfill 'to' date must be on or after 'from' date")
    days = (end - start).days + 1
    if days > MAX_BACKFILL_DAYS:
        raise ValueError(f"A backfill covers at most {MAX_BACKFILL_DAYS} days; "
                         f"this range is {days}. Queue it in parts.")
    existing = set(db.scalars(select(WorkflowRun.run_key).where(
        WorkflowRun.run_key.like(f"backfill:{workflow.id}:%")
    )))
    runs = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        key = f"backfill:{workflow.id}:{day.isoformat()}"
        if key not in existing:
            # Relative date defaults ("yesterday") count from the day backfilled.
            values = _no_secrets(resolve(workflow.run_parameters_json,
                                         {**(params or {}), "ds": day.isoformat()}, day=day))
            runs.append(WorkflowRun(workflow_id=workflow.id, trigger_type=TriggerType.backfill,
                                    status="queued", parameters_json=values, run_key=key))
    db.add_all(runs)
    try:
        db.commit()
    except IntegrityError:
        # Two requests for overlapping ranges at once: the second loses on the
        # unique run_key. Say so rather than answering with a traceback.
        db.rollback()
        raise ValueError("Another backfill of these dates was queued at the same time; "
                         "check the run list and retry for any dates still missing") from None
    for run in runs:
        db.refresh(run)
    return runs
