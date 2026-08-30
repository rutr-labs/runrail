"""Workflow-as-code: export workflows to YAML-friendly dicts and apply them back.

Files reference projects and environments by *name* (never by id) so a file
exported on one machine applies cleanly on another. Apply is a declarative
upsert keyed on workflow name: tasks present in the file are created/updated,
tasks missing from the file are removed from that workflow.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from runrail.models import Environment, Project, Task, Workflow
from runrail.schemas import TaskIn

# Configuration only. Operator state (snooze, notification markers, breach
# markers) is deliberately absent: a file applied on another machine must not
# carry one operator's "quiet until Monday".
_WORKFLOW_FIELDS = ("description", "schedule_cron", "schedule_timezone", "enabled",
                    "max_concurrent_runs", "notify_webhook_url", "auto_pause_failures",
                    "missed_run_grace_minutes", "sla_minutes")
_TASK_FIELDS = ("task_type", "command", "script_path", "notebook_path", "sql_path", "cwd",
                "retries", "retry_delay_seconds", "timeout_seconds",
                "requires_approval", "approval_prompt")
#: Values for task fields a file omits. NOT NULL columns must appear here, or
#: the apply loop writes None into them.
_TASK_DEFAULTS = {"retries": 0, "retry_delay_seconds": 60, "requires_approval": False}


def _compact(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items()
            if value not in (None, [], {})}


def export_workflows(db: Session, name: str | None = None) -> dict[str, Any]:
    stmt = select(Workflow).options(selectinload(Workflow.tasks)).order_by(Workflow.name)
    if name:
        stmt = stmt.where(Workflow.name == name)
    workflows = db.scalars(stmt).all()
    if name and not workflows:
        raise ValueError(f"Workflow '{name}' does not exist")
    projects = {p.id: p.name for p in db.scalars(select(Project))}
    environments = {e.id: e.name for e in db.scalars(select(Environment))}

    exported = []
    for workflow in workflows:
        item: dict[str, Any] = {"name": workflow.name}
        item.update(_compact({field: getattr(workflow, field) for field in _WORKFLOW_FIELDS}))
        item["enabled"] = workflow.enabled  # always explicit, even when False
        if workflow.project_id:
            item["project"] = projects.get(workflow.project_id)
        if workflow.default_environment_id:
            item["default_environment"] = environments.get(workflow.default_environment_id)
        tasks = []
        for task in sorted(workflow.tasks, key=lambda t: t.id):
            entry: dict[str, Any] = {"name": task.name, "task_type": task.task_type.value}
            entry.update(_compact({field: getattr(task, field) for field in _TASK_FIELDS
                                   if field != "task_type"}))
            if task.depends_on_json:
                entry["depends_on"] = list(task.depends_on_json)
            if task.parameters_json:
                entry["parameters"] = dict(task.parameters_json)
            if task.project_id:
                entry["project"] = projects.get(task.project_id)
            if task.environment_id:
                entry["environment"] = environments.get(task.environment_id)
            tasks.append(entry)
        item["tasks"] = tasks
        exported.append(item)
    return {"workflows": exported}


def _resolve(db: Session, model, name: str | None, kind: str) -> int | None:
    if not name:
        return None
    obj = db.scalar(select(model).where(model.name == name))
    if obj is None:
        available = ", ".join(db.scalars(select(model.name)).all()) or "none defined"
        raise ValueError(f"Unknown {kind} '{name}' (available: {available})")
    return obj.id


def _validate_graph(tasks: list[dict[str, Any]]) -> None:
    names = [task["name"] for task in tasks]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate task names in workflow")
    known = set(names)
    graph = {task["name"]: set(task.get("depends_on") or []) for task in tasks}
    for name, deps in graph.items():
        unknown = deps - known
        if unknown:
            raise ValueError(f"Task '{name}' depends on unknown tasks: {', '.join(sorted(unknown))}")
    resolved: set[str] = set()
    remaining = dict(graph)
    while remaining:
        ready = [name for name, deps in remaining.items() if deps <= resolved]
        if not ready:
            raise ValueError("Task dependency graph contains a cycle")
        for name in ready:
            resolved.add(name)
            del remaining[name]


def apply_workflows(db: Session, data: dict[str, Any]) -> dict[str, list[str]]:
    entries = data.get("workflows")
    if not isinstance(entries, list) or not entries:
        raise ValueError("File must contain a top-level 'workflows' list")
    summary: dict[str, list[str]] = {"created": [], "updated": []}
    for entry in entries:
        wf_name = entry.get("name")
        if not wf_name:
            raise ValueError("Every workflow needs a name")
        task_entries = entry.get("tasks") or []
        _validate_graph(task_entries)
        # Validate every task through the same schema the API uses.
        for task_entry in task_entries:
            TaskIn(**{
                "name": task_entry["name"], "task_type": task_entry.get("task_type"),
                "command": task_entry.get("command"),
                "script_path": task_entry.get("script_path"),
                "notebook_path": task_entry.get("notebook_path"),
                "sql_path": task_entry.get("sql_path"), "cwd": task_entry.get("cwd"),
                "depends_on_json": task_entry.get("depends_on") or [],
                "parameters_json": task_entry.get("parameters"),
                "retries": task_entry.get("retries", 0),
                "retry_delay_seconds": task_entry.get("retry_delay_seconds", 60),
                "timeout_seconds": task_entry.get("timeout_seconds"),
                "requires_approval": bool(task_entry.get("requires_approval", False)),
                "approval_prompt": task_entry.get("approval_prompt"),
            })

        workflow = db.scalar(select(Workflow).where(Workflow.name == wf_name))
        created = workflow is None
        if created:
            workflow = Workflow(name=wf_name)
            db.add(workflow)
        workflow.description = entry.get("description")
        workflow.schedule_cron = entry.get("schedule_cron")
        workflow.schedule_timezone = entry.get("schedule_timezone")
        workflow.enabled = bool(entry.get("enabled", True))
        workflow.max_concurrent_runs = int(entry.get("max_concurrent_runs", 1))
        workflow.notify_webhook_url = entry.get("notify_webhook_url")
        workflow.auto_pause_failures = entry.get("auto_pause_failures")
        workflow.missed_run_grace_minutes = entry.get("missed_run_grace_minutes")
        workflow.sla_minutes = entry.get("sla_minutes")
        workflow.project_id = _resolve(db, Project, entry.get("project"), "project")
        workflow.default_environment_id = _resolve(
            db, Environment, entry.get("default_environment"), "environment")
        db.flush()

        existing = {task.name: task for task in db.scalars(
            select(Task).where(Task.workflow_id == workflow.id))}
        wanted = set()
        for task_entry in task_entries:
            wanted.add(task_entry["name"])
            task = existing.get(task_entry["name"]) or Task(
                workflow_id=workflow.id, name=task_entry["name"])
            task.task_type = task_entry["task_type"]
            for field in _TASK_FIELDS:
                if field != "task_type":
                    setattr(task, field, task_entry.get(field, _TASK_DEFAULTS.get(field)))
            task.depends_on_json = list(task_entry.get("depends_on") or [])
            task.parameters_json = task_entry.get("parameters")
            task.project_id = _resolve(db, Project, task_entry.get("project"), "project")
            task.environment_id = _resolve(
                db, Environment, task_entry.get("environment"), "environment")
            db.add(task)
        for name, task in existing.items():
            if name not in wanted:
                db.delete(task)
        summary["created" if created else "updated"].append(wf_name)
    db.commit()
    return summary
