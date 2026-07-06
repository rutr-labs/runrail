from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased

from runrail.models import RunStatus, Workflow, WorkflowRun, now


def _running_runs_for_same_workflow():
    """Correlated count of currently running runs for the candidate's workflow."""
    other = aliased(WorkflowRun)
    return (
        select(func.count())
        .select_from(other)
        .where(
            other.workflow_id == WorkflowRun.workflow_id,
            other.status == RunStatus.running,
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
    ).values(status=RunStatus.running, started_at=now()))
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(WorkflowRun, candidate)
