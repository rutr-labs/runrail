from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runrail.models import Environment, EnvironmentStatus, TriggerType, Workflow, WorkflowRun


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


def create_run(db: Session, workflow: Workflow, trigger: TriggerType, params=None, run_key=None):
    return save(db, WorkflowRun(
        workflow_id=workflow.id, status="queued", trigger_type=trigger,
        parameters_json=params or {}, run_key=run_key,
    ))


def create_backfill(db: Session, workflow: Workflow, start: date, end: date, params=None):
    if end < start:
        raise HTTPException(400, "Backfill 'to' date must be on or after 'from' date")
    existing = set(db.scalars(select(WorkflowRun.run_key).where(
        WorkflowRun.run_key.like(f"backfill:{workflow.id}:%")
    )))
    runs = []
    day = start
    while day <= end:
        key = f"backfill:{workflow.id}:{day.isoformat()}"
        if key not in existing:
            values = dict(params or {})
            values["ds"] = day.isoformat()
            runs.append(WorkflowRun(workflow_id=workflow.id, trigger_type=TriggerType.backfill,
                                    status="queued", parameters_json=values, run_key=key))
        day += timedelta(days=1)
    db.add_all(runs)
    db.commit()
    for run in runs:
        db.refresh(run)
    return runs
