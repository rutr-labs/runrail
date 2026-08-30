"""Run control: resume a failed run, and approve or reject an open gate."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from runrail.api.crud import get_or_404
from runrail.api.routes_workflows import _ensure_workflow_runnable
from runrail.api.ws import manager as ws_manager
from runrail.db import get_db
from runrail.models import (
    RunStatus,
    Task,
    TaskRun,
    TaskRunStatus,
    Workflow,
    WorkflowRun,
    now,
)
from runrail.notify import notify_approval_rejected
from runrail.schemas import ApprovalDecision, ResumeIn, TaskRunOut, WorkflowRunOut
from runrail.worker.service import resume_plan

router = APIRouter(prefix="/api", tags=["run-control"])

_RESUMABLE = (RunStatus.failed, RunStatus.cancelled)


def _workflow_tasks(db: Session, run: WorkflowRun) -> list[Task]:
    """The workflow's tasks as they are NOW — a resume executes the current
    definition, not the one the run started with."""
    return list(db.scalars(select(Task).where(Task.workflow_id == run.workflow_id)))


@router.get("/runs/{object_id}/resume-plan")
def get_resume_plan(object_id: int, rerun: list[str] = Query(default=[]),
                    db: Session = Depends(get_db)):
    """What a resume would reuse and re-run, with a reason per re-run task.

    200 with resumable=false for a live or successful run, so the UI can grey
    the button without an error toast.
    """
    run = get_or_404(db, WorkflowRun, object_id)
    try:
        return resume_plan(db, run, _workflow_tasks(db, run), tuple(rerun))
    except ValueError as exc:  # the graph was edited into an invalid state
        raise HTTPException(409, str(exc)) from exc


@router.post("/runs/{object_id}/resume", response_model=WorkflowRunOut)
def resume_run(object_id: int, data: ResumeIn | None = None, db: Session = Depends(get_db)):
    """Reopen this run and re-execute only what did not succeed.

    Deliberately not a new run: `ds`, `run_key` and `artifacts_dir` all derive
    from the run, so a fresh row would render yesterday's failure with today's
    date and point at an empty artifacts directory.
    """
    run = get_or_404(db, WorkflowRun, object_id)
    if run.status not in _RESUMABLE:
        raise HTTPException(409, f"Run is {run.status.value}; only a failed or "
                                 "cancelled run can be resumed")
    workflow = get_or_404(db, Workflow, run.workflow_id)
    _ensure_workflow_runnable(db, workflow)
    tasks = _workflow_tasks(db, run)
    forced = {task.name for task in tasks} & set(data.rerun if data else ())
    segment = run.resume_count + 1
    # Guarded so a double-click cannot open two segments. started_at opens a
    # fresh segment (queue.py re-stamps it); sla_breached_at is cleared so a
    # second breach can alert again.
    result = db.execute(update(WorkflowRun)
                        .where(WorkflowRun.id == run.id, WorkflowRun.status.in_(_RESUMABLE))
                        .values(status=RunStatus.queued, resume_count=segment, started_at=None,
                                finished_at=None, duration_seconds=None, sla_breached_at=None))
    if result.rowcount != 1:
        raise HTTPException(409, "Run is no longer resumable")
    # The worker recomputes the reuse set at claim time and never sees this
    # request body, so a forced re-run has to become data: a decided row in the
    # new segment that is not a success, which the walk reads like any failure.
    for task in tasks:
        if task.name in forced:
            db.add(TaskRun(workflow_run_id=run.id, task_id=task.id, attempt=0,
                           status=TaskRunStatus.cancelled, resume_index=segment,
                           error_message="Re-run requested at resume"))
    db.commit()
    db.refresh(run)
    ws_manager.notify({"type": "run_updated", "id": run.id})
    return run


@router.get("/approvals")
def list_approvals(db: Session = Depends(get_db)):
    """Every actionable gate, oldest first — how the dashboard and the run page
    find the gate id to decide.

    Gates on a live run are listed too, not just parked ones: a second branch
    can still be executing when the first opens its gate, and the approver
    should not have to wait for it. Gates left on a terminal run (cancelled
    while one was open) are not decisions anyone can still make.
    """
    rows = db.execute(
        select(TaskRun, Task, WorkflowRun, Workflow)
        .join(Task, Task.id == TaskRun.task_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(TaskRun.status == TaskRunStatus.awaiting_approval,
               WorkflowRun.status.in_((RunStatus.running, RunStatus.waiting_approval)))
        .order_by(TaskRun.created_at)
    ).all()
    return [TaskRunOut.model_validate(gate).model_dump() | {
        "task_run_id": gate.id, "run_id": run.id, "workflow_id": workflow.id,
        "workflow_name": workflow.name, "prompt": task.approval_prompt,
    } for gate, task, run, workflow in rows]


def _decide(db: Session, object_id: int, approved: bool,
            data: ApprovalDecision | None) -> TaskRun:
    gate = get_or_404(db, TaskRun, object_id)
    decided = now()
    result = db.execute(update(TaskRun)
                        .where(TaskRun.id == gate.id,
                               TaskRun.status == TaskRunStatus.awaiting_approval)
                        .values(status=TaskRunStatus.approved if approved else TaskRunStatus.rejected,
                                approval_note=data.note if data else None,
                                approved_at=decided, finished_at=decided))
    if result.rowcount != 1:
        raise HTTPException(409, f"This gate is already {gate.status.value}")
    db.commit()
    run = get_or_404(db, WorkflowRun, gate.workflow_run_id)
    open_gates = db.scalar(select(func.count()).select_from(TaskRun).where(
        TaskRun.workflow_run_id == run.id, TaskRun.status == TaskRunStatus.awaiting_approval))
    if not open_gates:
        # Re-enter once, when every gate in the run is decided: two parallel
        # gates resumed on the first decision would re-claim the run only to
        # park it again. A rejection re-enters too — the executor writes the
        # skipped rows downstream and lands the run cancelled.
        db.execute(update(WorkflowRun)
                   .where(WorkflowRun.id == run.id,
                          WorkflowRun.status == RunStatus.waiting_approval)
                   .values(status=RunStatus.queued))
        db.commit()
        ws_manager.notify({"type": "run_updated", "id": run.id})
    db.refresh(gate)
    ws_manager.notify({"type": "task_run_updated", "id": gate.id, "run_id": run.id})
    if not approved:
        notify_approval_rejected(db, gate)
    return gate


@router.post("/task-runs/{object_id}/approve", response_model=TaskRunOut)
def approve_gate(object_id: int, data: ApprovalDecision | None = None,
                 db: Session = Depends(get_db)):
    """Let the gated task run; the optional body records why."""
    return _decide(db, object_id, True, data)


@router.post("/task-runs/{object_id}/reject", response_model=TaskRunOut)
def reject_gate(object_id: int, data: ApprovalDecision | None = None,
                db: Session = Depends(get_db)):
    """Refuse the gated task: everything downstream is skipped and the run lands
    cancelled, not failed — a human decision is not a system failure."""
    return _decide(db, object_id, False, data)
