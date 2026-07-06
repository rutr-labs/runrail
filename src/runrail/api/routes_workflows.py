from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.api.crud import (
    apply_update,
    create_backfill,
    create_run,
    ensure_environment_ready,
    get_or_404,
    save,
)
from runrail.api.ws import manager as ws_manager
from runrail.db import get_db
from runrail.models import Project, Task, TaskType, TriggerType, Workflow
from runrail.schemas import (
    BackfillCreate,
    RunCreate,
    TaskIn,
    TaskOut,
    WorkflowIn,
    WorkflowOut,
    WorkflowRunOut,
)

router = APIRouter(prefix="/api")


def _validate_dependencies(
    db: Session, workflow_id: int, name: str, depends_on: list[str],
    exclude_task_id: int | None = None,
) -> None:
    """Reject dependencies on unknown tasks and dependency cycles at write time."""
    tasks = db.scalars(select(Task).where(Task.workflow_id == workflow_id)).all()
    graph = {t.name: set(t.depends_on_json or []) for t in tasks if t.id != exclude_task_id}
    graph[name] = set(depends_on)
    unknown = sorted(dep for dep in depends_on if dep != name and dep not in graph)
    if unknown:
        raise HTTPException(422, f"Unknown task dependencies: {', '.join(unknown)}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def has_cycle(node: str) -> bool:
        if node in visited: return False
        if node in visiting: return True
        visiting.add(node)
        if any(has_cycle(dep) for dep in graph.get(node, ())): return True
        visiting.discard(node); visited.add(node)
        return False

    if has_cycle(name):
        raise HTTPException(422, "These dependencies would create a cycle")


def _ensure_workflow_runnable(db: Session, workflow: Workflow) -> None:
    project = db.get(Project, workflow.project_id) if workflow.project_id else None
    tasks = db.scalars(select(Task).where(Task.workflow_id == workflow.id)).all()
    if not tasks:
        raise HTTPException(409, "Add at least one task before running this workflow")
    for task in tasks:
        task_project = db.get(Project, task.project_id) if task.project_id else project
        environment_id = (
            task.environment_id
            or workflow.default_environment_id
            or (task_project.default_environment_id if task_project else None)
        )
        if task.task_type in (TaskType.python, TaskType.notebook) and environment_id is None:
            raise HTTPException(
                409, f"Task '{task.name}' requires an execution environment"
            )
        ensure_environment_ready(db, environment_id)


@router.get("/workflows", response_model=list[WorkflowOut])
def list_workflows(db: Session = Depends(get_db)):
    return db.scalars(select(Workflow).order_by(Workflow.name)).all()


@router.post("/workflows", response_model=WorkflowOut, status_code=201)
def create_workflow(data: WorkflowIn, db: Session = Depends(get_db)):
    ensure_environment_ready(db, data.default_environment_id)
    return save(db, Workflow(**data.model_dump()))


@router.get("/workflows/{object_id}", response_model=WorkflowOut)
def get_workflow(object_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, Workflow, object_id)


@router.put("/workflows/{object_id}", response_model=WorkflowOut)
def update_workflow(object_id: int, data: WorkflowIn, db: Session = Depends(get_db)):
    ensure_environment_ready(db, data.default_environment_id)
    return save(db, apply_update(get_or_404(db, Workflow, object_id), data.model_dump()))


@router.delete("/workflows/{object_id}", status_code=204)
def delete_workflow(object_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Workflow, object_id)); db.commit()
    return Response(status_code=204)


@router.post("/workflows/{object_id}/run", response_model=WorkflowRunOut, status_code=201)
def run_workflow(object_id: int, data: RunCreate, db: Session = Depends(get_db)):
    workflow = get_or_404(db, Workflow, object_id)
    _ensure_workflow_runnable(db, workflow)
    run = create_run(db, workflow, TriggerType.manual, data.parameters)
    ws_manager.notify({"type": "run_created", "id": run.id, "workflow_id": run.workflow_id})
    return run


@router.post("/workflows/{object_id}/backfill", response_model=list[WorkflowRunOut], status_code=201)
def backfill_workflow(object_id: int, data: BackfillCreate, db: Session = Depends(get_db)):
    workflow = get_or_404(db, Workflow, object_id)
    _ensure_workflow_runnable(db, workflow)
    runs = create_backfill(db, workflow, data.from_date, data.to_date, data.parameters)
    for run in runs:
        ws_manager.notify({"type": "run_created", "id": run.id, "workflow_id": run.workflow_id})
    return runs


@router.get("/workflows/{workflow_id}/tasks", response_model=list[TaskOut])
def list_tasks(workflow_id: int, db: Session = Depends(get_db)):
    get_or_404(db, Workflow, workflow_id)
    return db.scalars(select(Task).where(Task.workflow_id == workflow_id).order_by(Task.id)).all()


@router.post("/workflows/{workflow_id}/tasks", response_model=TaskOut, status_code=201)
def create_task(workflow_id: int, data: TaskIn, db: Session = Depends(get_db)):
    get_or_404(db, Workflow, workflow_id)
    ensure_environment_ready(db, data.environment_id)
    _validate_dependencies(db, workflow_id, data.name, data.depends_on_json)
    return save(db, Task(workflow_id=workflow_id, **data.model_dump()))


@router.get("/tasks/{object_id}", response_model=TaskOut)
def get_task(object_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, Task, object_id)


@router.put("/tasks/{object_id}", response_model=TaskOut)
def update_task(object_id: int, data: TaskIn, db: Session = Depends(get_db)):
    task = get_or_404(db, Task, object_id)
    ensure_environment_ready(db, data.environment_id)
    old_name = task.name
    if task.workflow_id is not None:
        _validate_dependencies(
            db, task.workflow_id, data.name, data.depends_on_json, exclude_task_id=task.id
        )
        if data.name != old_name:  # keep sibling dependencies pointing at the renamed task
            for sibling in db.scalars(select(Task).where(Task.workflow_id == task.workflow_id,
                                                         Task.id != task.id)):
                if old_name in (sibling.depends_on_json or []):
                    sibling.depends_on_json = [
                        data.name if dep == old_name else dep for dep in sibling.depends_on_json
                    ]
    return save(db, apply_update(task, data.model_dump()))


@router.delete("/tasks/{object_id}", status_code=204)
def delete_task(object_id: int, db: Session = Depends(get_db)):
    task = get_or_404(db, Task, object_id)
    if task.workflow_id is not None:  # drop the deleted task from sibling dependency lists
        for sibling in db.scalars(select(Task).where(Task.workflow_id == task.workflow_id,
                                                     Task.id != task.id)):
            if task.name in (sibling.depends_on_json or []):
                sibling.depends_on_json = [d for d in sibling.depends_on_json if d != task.name]
    db.delete(task); db.commit()
    return Response(status_code=204)
