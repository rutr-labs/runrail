from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from runrail.models import LockMode, RunStatus, Workflow, WorkflowRun, now

#: The statuses that are unconditionally mid-flight. `queued` belongs to the
#: same window but only sometimes, so it is not a member here — see
#: _occupies_slot.
_OCCUPIES_SLOT = (RunStatus.running, RunStatus.waiting_approval)


def _occupies_slot(run):
    """A run is mid-flight — holding its workflow's concurrency budget and its
    named resource — from its first claim until it lands terminal.

    A run parked on an approval gate holds both: it has partial state and will
    resume into the same tasks. So does one a decision has just handed back to
    `queued`; only the re-claim is missing, and the half-written state is still
    there. Releasing either in that window lets the next scheduled iteration
    start underneath it and race it over the same outputs.

    started_at is what separates that re-entry from a run that has never
    executed: the claim stamps it once and never rewrites it, and a resume nulls
    it to open a fresh segment — so a resumed run is new again.
    """
    return or_(run.status.in_(_OCCUPIES_SLOT),
               and_(run.status == RunStatus.queued, run.started_at.is_not(None)))


def _running_runs_for_same_workflow():
    """Correlated count of OTHER runs holding a concurrency slot for the
    candidate's workflow. The candidate is excluded because it may now hold one
    itself, and a re-entering run must not be barred by its own claim."""
    other = aliased(WorkflowRun)
    return (
        select(func.count())
        .select_from(other)
        .where(
            other.workflow_id == WorkflowRun.workflow_id,
            other.id != WorkflowRun.id,
            _occupies_slot(other),
        )
        .correlate(WorkflowRun)
        .scalar_subquery()
    )


def _candidate_workflow(column):
    """The candidate's own workflow column, usable where Workflow is not joined."""
    return (
        select(column)
        .where(Workflow.id == WorkflowRun.workflow_id)
        .correlate(WorkflowRun)
        .scalar_subquery()
    )


def _resource_blocked():
    """EXISTS a run that denies the candidate its workflow's named resource.

    A run HOLDS the resource for as long as it occupies a concurrency slot: a run
    parked on an approval gate has partial state and resumes into the same tasks,
    so releasing its lock would let a second writer at the same database.
    """
    holder, holder_workflow = aliased(WorkflowRun), aliased(Workflow)
    resource = _candidate_workflow(Workflow.lock_resource)
    mode = _candidate_workflow(Workflow.lock_mode)
    holder_is_exclusive = holder_workflow.lock_mode == LockMode.exclusive
    return (
        select(1)
        .select_from(holder)
        .join(holder_workflow, holder_workflow.id == holder.workflow_id)
        .where(
            # NULL equals nothing in SQL, so a workflow without a resource is
            # blocked by no one and blocks no one.
            holder_workflow.lock_resource == resource,
            # A run re-entering mid-segment is its own holder: it must not be
            # barred by the resource it is already holding.
            holder.id != WorkflowRun.id,
            or_(
                and_(_occupies_slot(holder),
                     or_(holder_is_exclusive, mode == LockMode.exclusive)),
                # Starvation guard: while an exclusive run waits its turn, no NEW
                # shared run may start ahead of it — otherwise a steady drip of
                # hourly jobs means the monthly maintenance never runs. Shared runs
                # already holding the resource finish untouched: a barrier, never a
                # preemption — and a shared run that is mid-segment (started_at
                # stamped) is one of those, not a new arrival.
                and_(holder.status == RunStatus.queued, holder_is_exclusive,
                     mode == LockMode.shared, WorkflowRun.started_at.is_(None)),
            ),
        )
        .correlate(WorkflowRun)
        .exists()
    )


def claim_next_run(db: Session) -> WorkflowRun | None:
    """Atomically claim the oldest queued run whose workflow has concurrency budget
    left and can take its named resource.

    Different workflows execute in parallel. Runs of the same workflow respect its
    max_concurrent_runs (default 1), so a long run never blocks other workflows and
    later iterations of the same workflow wait in the queue. A lock_resource narrows
    that further, across workflows: exclusive runs get the resource alone, shared
    runs overlap each other.
    """
    candidate = db.scalar(
        select(WorkflowRun.id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            WorkflowRun.status == RunStatus.queued,
            _running_runs_for_same_workflow() < Workflow.max_concurrent_runs,
            ~_resource_blocked(),
        )
        .order_by(WorkflowRun.created_at)
        .limit(1)
    )
    if candidate is None:
        return None
    workflow_limit = _candidate_workflow(Workflow.max_concurrent_runs)
    # Re-check every condition inside the UPDATE so concurrent workers cannot
    # exceed the per-workflow limit, double-claim the same run, or both acquire
    # the same resource.
    result = db.execute(update(WorkflowRun).where(
        WorkflowRun.id == candidate,
        WorkflowRun.status == RunStatus.queued,
        _running_runs_for_same_workflow() < workflow_limit,
        ~_resource_blocked(),
    # coalesce, not now(): a run can be claimed more than once — an approval
    # gate releases it back to `queued` while a human decides, and re-entry
    # must not rewrite the run's start time and walk the timeline origin
    # forward. A resume deliberately nulls started_at first to open a fresh
    # segment, so it still gets a new one.
    ).values(status=RunStatus.running,
             started_at=func.coalesce(WorkflowRun.started_at, now())))
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(WorkflowRun, candidate)
