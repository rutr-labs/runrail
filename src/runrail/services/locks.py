"""Named resource locks between services and workflow runs.

One namespace: a service holding `gpu` and a workflow locking `gpu` exclude
each other exactly as two workflows do. Exclusive excludes everything; shared
holders coexist with each other.

A service HOLDS its resource from the moment it is claimed to start until its
process is gone — while stopping too, because a model server finishing its
last request is still using the GPU. A service WAITING for its resource bars
new runs from taking it, so a steady stream of GPU jobs cannot starve a model
whose window has opened. Nothing is ever preempted: a service whose window
opens waits for the runs holding the resource, and runs wait for services.

Both sides acquire through one conditional statement each — the run claim's
UPDATE and the service's — so on SQLite, which serialises writers, two can
never both take an exclusive resource.
"""

from datetime import datetime

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import Session, aliased

from runrail.models import (
    LockMode,
    RunStatus,
    Service,
    ServiceState,
    Workflow,
    WorkflowRun,
)

#: States in which a service holds its resource.
HOLDING = (ServiceState.starting, ServiceState.running, ServiceState.stopping)


def service_blocks_run():
    """EXISTS a service denying the run being claimed its workflow's resource.

    Correlated to WorkflowRun, for runrail.worker.queue.claim_next_run."""
    resource = (select(Workflow.lock_resource).where(Workflow.id == WorkflowRun.workflow_id)
                .correlate(WorkflowRun).scalar_subquery())
    mode = (select(Workflow.lock_mode).where(Workflow.id == WorkflowRun.workflow_id)
            .correlate(WorkflowRun).scalar_subquery())
    return (
        select(1).select_from(Service)
        .where(
            Service.lock_resource == resource,  # NULL matches nothing
            or_(Service.lock_mode == LockMode.exclusive, mode == LockMode.exclusive),
            or_(Service.state.in_(HOLDING),
                # Waiting services bar NEW runs only; a run re-entering
                # mid-segment already holds the resource.
                and_(Service.state == ServiceState.waiting_lock,
                     WorkflowRun.started_at.is_(None))),
        )
        .correlate(WorkflowRun)
        .exists()
    )


def _conflicts(service: Service, waiting_since: datetime | None):
    """Conditions under which `service` may not take its resource now."""
    exclusive = service.lock_mode == LockMode.exclusive
    run, flow = aliased(WorkflowRun), aliased(Workflow)
    holds_slot = or_(run.status.in_((RunStatus.running, RunStatus.waiting_approval)),
                     and_(run.status == RunStatus.queued, run.started_at.is_not(None)))
    runs = (
        exists().where(
            flow.id == run.workflow_id,
            flow.lock_resource == service.lock_resource,
            or_(
                and_(holds_slot, or_(flow.lock_mode == LockMode.exclusive, exclusive)),
                # The runs' own starvation guard, applied to a shared service:
                # an exclusive run waiting its turn is not overtaken.
                and_(run.status == RunStatus.queued, run.started_at.is_(None),
                     flow.lock_mode == LockMode.exclusive, not exclusive),
            ),
        )
    )
    other = aliased(Service)
    waiting_first = (other.state == ServiceState.waiting_lock if waiting_since is None
                     else and_(other.state == ServiceState.waiting_lock,
                               other.state_since < waiting_since))
    services = exists().where(
        other.id != service.id,
        other.lock_resource == service.lock_resource,
        or_(other.lock_mode == LockMode.exclusive, exclusive),
        # First come, first served among services waiting for the same resource.
        or_(other.state.in_(HOLDING), waiting_first),
    )
    return or_(runs, services)


def try_claim(db: Session, service: Service, now: datetime) -> bool:
    """Atomically move a service to `starting` if its resource is free (or it
    has none). Returns whether it was claimed; commits either way."""
    waiting_since = service.state_since if service.state == ServiceState.waiting_lock else None
    conditions = [Service.id == service.id,
                  Service.state.not_in(HOLDING)]
    if service.lock_resource:
        conditions.append(~_conflicts(service, waiting_since))
    claimed = db.execute(update(Service).where(*conditions).values(
        state=ServiceState.starting, state_since=now, state_detail=None,
        next_attempt_at=None)).rowcount == 1
    db.commit()
    return claimed


def holder_description(db: Session, service: Service) -> str | None:
    """Who has the resource a waiting service needs, for its status line."""
    if not service.lock_resource:
        return None
    holder = db.scalar(select(Service.name).where(
        Service.id != service.id, Service.lock_resource == service.lock_resource,
        Service.state.in_(HOLDING)).limit(1))
    if holder:
        return f"service {holder}"
    workflow = db.scalar(
        select(Workflow.name).join(WorkflowRun, WorkflowRun.workflow_id == Workflow.id).where(
            Workflow.lock_resource == service.lock_resource,
            or_(WorkflowRun.status.in_((RunStatus.running, RunStatus.waiting_approval)),
                and_(WorkflowRun.status == RunStatus.queued,
                     WorkflowRun.started_at.is_not(None)))).limit(1))
    if workflow:
        return f"workflow {workflow}"
    return None
