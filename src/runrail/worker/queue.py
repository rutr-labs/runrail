from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased

from runrail.models import RunStatus, Workflow, WorkflowRun, now

#: A run parked on an approval gate still occupies its workflow's concurrency
#: budget — it holds partial state and will resume into the same tasks. Counting
#: only `running` would let the next scheduled iteration start underneath it and
#: race the approved one over the same outputs.
_OCCUPIES_SLOT = (RunStatus.running, RunStatus.waiting_approval)


def _running_runs_for_same_workflow():
    """Correlated count of runs holding a concurrency slot for the candidate's workflow."""
    other = aliased(WorkflowRun)
    return (
        select(func.count())
        .select_from(other)
        .where(
            other.workflow_id == WorkflowRun.workflow_id,
            other.status.in_(_OCCUPIES_SLOT),
        )
        .correlate(WorkflowRun)
        .scalar_subquery()
    )


def claim_next_run(db: Session) -> WorkflowRun | None:
    """Atomically claim the oldest queued run whose workflow has concurrency budget left.

    Different workflows execute in parallel. Runs of the same workflow respect its
    max_concurrent_runs (default 1), so a long run never blocks other workflows and
    later iterations of the same workflow wait in the queue.
    """
    candidate = db.scalar(
        select(WorkflowRun.id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            WorkflowRun.status == RunStatus.queued,
            _running_runs_for_same_workflow() < Workflow.max_concurrent_runs,
        )
        .order_by(WorkflowRun.created_at)
        .limit(1)
    )
    if candidate is None:
        return None
    workflow_limit = (
        select(Workflow.max_concurrent_runs)
        .where(Workflow.id == WorkflowRun.workflow_id)
        .correlate(WorkflowRun)
        .scalar_subquery()
    )
    # Re-check both conditions inside the UPDATE so concurrent workers cannot
    # exceed the per-workflow limit or double-claim the same run.
    result = db.execute(update(WorkflowRun).where(
        WorkflowRun.id == candidate,
        WorkflowRun.status == RunStatus.queued,
        _running_runs_for_same_workflow() < workflow_limit,
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
