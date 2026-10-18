"""Workflow-as-code: export workflows to YAML-friendly dicts and apply them back.

Files reference projects and environments by *name* (never by id) so a file
exported on one machine applies cleanly on another. Apply is a declarative
upsert keyed on the workflow's `key` — written into every export, so a file
applied after a rename in the UI updates that workflow (renaming it back) rather
than creating a duplicate — and on its name when a file has no key. Tasks
present in the file are created/updated, tasks missing from it are removed.
"""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from runrail.models import (
    Environment,
    LockMode,
    Project,
    RestartPolicy,
    Service,
    ServiceStartMode,
    ServiceState,
    Task,
    Workflow,
)
from runrail.schemas import ServiceIn, TaskIn, WorkflowIn

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

#: What a hand-written `key:` may look like; exports write a uuid4 hex.
_KEY = re.compile(r"[A-Za-z0-9_-]{1,32}")


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
        item: dict[str, Any] = {"name": workflow.name, "key": workflow.key}
        item.update(_compact({field: getattr(workflow, field) for field in _WORKFLOW_FIELDS}))
        item["enabled"] = workflow.enabled  # always explicit, even when False
        # Exported as a pair, and only as a pair: the mode alone is meaningless,
        # and yaml.safe_dump refuses an enum member, so write its value.
        if workflow.lock_resource:
            item["lock_resource"] = workflow.lock_resource
            item["lock_mode"] = workflow.lock_mode.value
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
    data: dict[str, Any] = {"workflows": exported}
    if not name:
        # Projects and environments ride along in a full export, so a file
        # applied to a fresh home can create what its workflows refer to. By
        # name only, the apply used to fail on the first task that named one.
        project_rows = [_compact({"name": p.name, "root_path": p.root_path,
                                  "description": p.description,
                                  "default_environment": environments.get(p.default_environment_id)})
                        for p in db.scalars(select(Project).order_by(Project.name))]
        environment_rows = [_export_environment(e)
                            for e in db.scalars(select(Environment).order_by(Environment.name))]
        if project_rows:
            data["projects"] = project_rows
        if environment_rows:
            data["environments"] = environment_rows
        services = [_export_service(service, projects, environments)
                    for service in db.scalars(select(Service).order_by(Service.name))]
        if services:
            data["services"] = services
    return data


def _export_environment(environment: Environment) -> dict[str, Any]:
    """Configuration only: a managed environment is its package list, an
    external one is where its interpreter lives on the machine it came from."""
    item: dict[str, Any] = {"name": environment.name}
    item.update(_compact({"description": environment.description,
                          "env_vars": environment.env_vars_json}))
    if environment.managed:
        item["managed"] = True
        item["packages"] = list(environment.packages_json or [])
    else:
        item["env_type"] = environment.env_type.value
        item.update(_compact({"executable": environment.executable,
                              "conda_env": environment.conda_env}))
    return item


def _export_service(service: Service, projects: dict, environments: dict) -> dict[str, Any]:
    item: dict[str, Any] = {"name": service.name, "key": service.key}
    item.update(_compact({
        "description": service.description, "command": service.command, "cwd": service.cwd,
        "env_vars": service.env_vars_json, "timezone": service.timezone,
        "notify_webhook_url": service.notify_webhook_url,
    }))
    item["start_mode"] = service.start_mode.value
    if service.windows_json:
        item["windows"] = [dict(window) for window in service.windows_json]
    item["restart_policy"] = service.restart_policy.value
    item["stop_timeout_seconds"] = service.stop_timeout_seconds
    if service.lock_resource:
        item["lock_resource"] = service.lock_resource
        item["lock_mode"] = service.lock_mode.value
    if service.project_id:
        item["project"] = projects.get(service.project_id)
    if service.environment_id:
        item["environment"] = environments.get(service.environment_id)
    return item


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
    entries = data.get("workflows") or []
    services = data.get("services") or []
    projects = data.get("projects") or []
    environments = data.get("environments") or []
    if not all(isinstance(section, list) for section in (entries, services, projects, environments)) \
            or not (entries or services or projects or environments):
        raise ValueError("File must contain a top-level 'workflows' or 'services' list")
    summary: dict[str, list[str]] = {"created": [], "updated": []}
    # Dependencies first, so the workflows and services below can name them.
    for entry in environments:
        created, env_name = _apply_environment(db, entry)
        summary["created" if created else "updated"].append(f"environment {env_name}")
    for entry in projects:
        created, project_name = _apply_project(db, entry)
        summary["created" if created else "updated"].append(f"project {project_name}")
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

        key = entry.get("key")
        if key is not None and not (isinstance(key, str) and _KEY.fullmatch(key)):
            raise ValueError(f"Workflow '{wf_name}': key must be 1-32 letters, digits, - or _")
        workflow = db.scalar(select(Workflow).where(Workflow.key == key)) if key else None
        if workflow is None:
            workflow = db.scalar(select(Workflow).where(Workflow.name == wf_name))
        created = workflow is None
        if created:
            workflow = Workflow(name=wf_name, **({"key": key} if key else {}))
            db.add(workflow)
        elif workflow.name != wf_name:
            # Matched by key after a rename: the file is the source of truth.
            if db.scalar(select(Workflow.id).where(Workflow.name == wf_name)) is not None:
                raise ValueError(f"Workflow '{workflow.name}' is '{wf_name}' in this file, "
                                 f"but another workflow already has that name")
            workflow.name = wf_name
        # Through the same schema as the API, like the tasks above. Writing the
        # columns straight from YAML accepted what the API refuses: an unknown
        # time zone the scheduler then skipped, and max_concurrent_runs: 0,
        # which queued runs forever while the missed-run watchdog stayed quiet.
        checked = WorkflowIn(
            name=wf_name, description=entry.get("description"),
            schedule_cron=entry.get("schedule_cron"),
            schedule_timezone=entry.get("schedule_timezone"),
            enabled=bool(entry.get("enabled", True)),
            max_concurrent_runs=entry.get("max_concurrent_runs", 1),
            notify_webhook_url=entry.get("notify_webhook_url"),
            auto_pause_failures=entry.get("auto_pause_failures"),
            missed_run_grace_minutes=entry.get("missed_run_grace_minutes"),
            sla_minutes=entry.get("sla_minutes"),
            lock_resource=entry.get("lock_resource"),
            lock_mode=entry.get("lock_mode") or LockMode.shared,
        )
        was_enabled = workflow.enabled if not created else None
        for field in ("description", "schedule_cron", "schedule_timezone", "enabled",
                      "max_concurrent_runs", "notify_webhook_url", "auto_pause_failures",
                      "missed_run_grace_minutes", "sla_minutes", "lock_resource", "lock_mode"):
            setattr(workflow, field, getattr(checked, field))
        if workflow.enabled and was_enabled is False:
            from runrail.api.routes_workflows import mark_enabled
            mark_enabled(workflow)
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
    for entry in services:
        created, name = _apply_service(db, entry)
        summary["created" if created else "updated"].append(f"service {name}")
    db.commit()
    return summary


def _apply_environment(db: Session, entry: dict[str, Any]) -> tuple[bool, str]:
    """Create an environment the file describes, or update its configuration.

    An external environment whose interpreter is not on this machine is still
    created — as `failed`, with the reason — rather than refusing the whole
    file: the workflows that name it can be applied now and the environment
    fixed from the Environments page. A managed one is created `creating` and
    the worker builds it. Nothing here touches an existing environment's build.
    """
    from runrail.environments import validate_external, validate_package_specs
    from runrail.models import EnvironmentStatus, EnvironmentType

    if not isinstance(entry, dict) or not entry.get("name"):
        raise ValueError("Every environment needs a name")
    name = entry["name"]
    environment = db.scalar(select(Environment).where(Environment.name == name))
    created = environment is None
    managed = bool(entry.get("managed"))
    if created:
        environment = Environment(name=name, managed=managed,
                                  status=EnvironmentStatus.creating if managed else EnvironmentStatus.ready)
        db.add(environment)
    environment.description = entry.get("description")
    environment.env_vars_json = entry.get("env_vars")
    if managed:
        if not created and not environment.managed:
            raise ValueError(f"Environment '{name}' exists here as an external interpreter; "
                             "the file describes a managed one")
        packages = validate_package_specs(list(entry.get("packages") or []))
        if created or packages != list(environment.packages_json or []):
            environment.packages_json = packages
            if not created:
                environment.status = EnvironmentStatus.creating  # a rebuild, picked up by the worker
    else:
        if not created and environment.managed:
            raise ValueError(f"Environment '{name}' exists here as a managed virtualenv; "
                             "the file describes an external one")
        environment.env_type = EnvironmentType(entry.get("env_type") or EnvironmentType.python)
        environment.executable = entry.get("executable")
        environment.conda_env = entry.get("conda_env")
        try:
            validate_external(environment)
        except ValueError as exc:
            environment.status = EnvironmentStatus.failed
            environment.last_error = str(exc)
    db.flush()
    return created, name


def _apply_project(db: Session, entry: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(entry, dict) or not entry.get("name"):
        raise ValueError("Every project needs a name")
    name = entry["name"]
    root = entry.get("root_path")
    if not root:
        raise ValueError(f"Project '{name}' needs a root_path")
    project = db.scalar(select(Project).where(Project.name == name))
    created = project is None
    if created:
        project = Project(name=name, root_path=str(root))
        db.add(project)
    project.root_path = str(root)
    project.description = entry.get("description")
    project.default_environment_id = _resolve(
        db, Environment, entry.get("default_environment"), "environment")
    db.flush()
    return created, name


def _apply_service(db: Session, entry: dict[str, Any]) -> tuple[bool, str]:
    """Upsert one service, by key then name, through the API's schema. Like a
    workflow, the file is the source of truth for its configuration; what the
    service is doing right now (its process, any Start/Stop override) is not
    configuration and is left alone."""
    if not isinstance(entry, dict) or not entry.get("name"):
        raise ValueError("Every service needs a name")
    name = entry["name"]
    checked = ServiceIn(
        name=name, description=entry.get("description"), command=entry.get("command") or "",
        cwd=entry.get("cwd"), env_vars_json=entry.get("env_vars"),
        start_mode=entry.get("start_mode") or ServiceStartMode.manual,
        windows=entry.get("windows") or [], timezone=entry.get("timezone"),
        restart_policy=entry.get("restart_policy"),
        stop_timeout_seconds=entry.get("stop_timeout_seconds", 30),
        lock_resource=entry.get("lock_resource"),
        lock_mode=entry.get("lock_mode") or LockMode.exclusive,
        notify_webhook_url=entry.get("notify_webhook_url"),
    )
    key = entry.get("key")
    if key is not None and not (isinstance(key, str) and _KEY.fullmatch(key)):
        raise ValueError(f"Service '{name}': key must be 1-32 letters, digits, - or _")
    service = db.scalar(select(Service).where(Service.key == key)) if key else None
    if service is None:
        service = db.scalar(select(Service).where(Service.name == name))
    created = service is None
    if created:
        service = Service(name=name, **({"key": key} if key else {}),
                          state=ServiceState.stopped, restart_requested=False)
        db.add(service)
    elif service.name != name:
        if db.scalar(select(Service.id).where(Service.name == name)) is not None:
            raise ValueError(f"Service '{service.name}' is '{name}' in this file, "
                             f"but another service already has that name")
        service.name = name
    for field in ("description", "command", "cwd", "env_vars_json", "start_mode", "timezone",
                  "stop_timeout_seconds", "lock_resource", "lock_mode", "notify_webhook_url"):
        setattr(service, field, getattr(checked, field))
    service.windows_json = checked.windows
    service.restart_policy = checked.restart_policy or (
        RestartPolicy.never if checked.start_mode == ServiceStartMode.manual
        else RestartPolicy.always)
    if not service.lock_resource:
        service.lock_mode = LockMode.exclusive
    service.project_id = _resolve(db, Project, entry.get("project"), "project")
    service.environment_id = _resolve(db, Environment, entry.get("environment"), "environment")
    db.flush()
    return created, name
