import json
import logging
import os
import re
import shutil
import signal
import socket
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, selectinload

from runrail import runtime_settings, variables
from runrail.api.ws import manager as _ws_manager
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.environments import (
    claim_next_environment,
    environment_python,
    environment_usable_now,
    managed_environment_path,
    provision_managed,
    queue_legacy_rebuilds,
)
from runrail.masking import mask_notebook
from runrail.models import (
    Artifact,
    ArtifactType,
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    Project,
    RunStatus,
    Task,
    TaskRun,
    TaskRunStatus,
    TaskType,
    TriggerType,
    WorkerBeat,
    Workflow,
    WorkflowRun,
    now,
)
from runrail.notify import notify_approval_requested, notify_run_outcome
from runrail.rundiff import capture_inputs
from runrail.worker.queue import claim_next_run
from runrail.worker.runners import (
    CommandSpec,
    build_command,
    display_command,
    execute_command,
    execute_sql,
    find_project_root,
    kill_running_tasks,
    python_module_name,
)
from runrail.worker.templating import render

log = logging.getLogger("runrail.worker")
#: After an error in the claim loop (a locked database, a dropped PostgreSQL
#: connection), how long the loop waits before trying again.
CLAIM_ERROR_BACKOFF_SECONDS = 2.0
#: How often a worker looks for runs abandoned by a worker that died.
RECOVER_EVERY_SECONDS = 30.0


def topological_tasks(tasks: list[Task]) -> list[Task]:
    by_name = {task.name: task for task in tasks}
    unknown = {dep for task in tasks for dep in (task.depends_on_json or []) if dep not in by_name}
    if unknown: raise ValueError(f"Unknown task dependencies: {', '.join(sorted(unknown))}")
    ordered, remaining = [], set(by_name)
    while remaining:
        ready = [name for name in remaining if set(by_name[name].depends_on_json or []) <= {t.name for t in ordered}]
        if not ready: raise ValueError("Task dependency graph contains a cycle")
        for name in sorted(ready, key=lambda n: by_name[n].id):
            ordered.append(by_name[name]); remaining.remove(name)
    return ordered


#: A gate is a TaskRun of its own. It never lands success: satisfied_outcomes
#: would then read the gate as the task itself and the approved work would be
#: skipped as "already done".
_GATE_STATUSES = (TaskRunStatus.awaiting_approval, TaskRunStatus.approved, TaskRunStatus.rejected)


def _latest_task_runs(db: Session, run: WorkflowRun, tasks: list[Task]) -> dict[str, TaskRun]:
    """Newest TaskRun per task name — the key depends_on_json and the executor's
    outcomes dict both use. Ordered by segment, then attempt, then id, so a
    gate (attempt 0) never outranks the execution it authorised."""
    names = {task.id: task.name for task in tasks}
    rows = db.scalars(select(TaskRun).where(TaskRun.workflow_run_id == run.id)
                      .order_by(TaskRun.resume_index, TaskRun.attempt, TaskRun.id)).all()
    return {names[row.task_id]: row for row in rows if row.task_id in names}


def _walk(tasks: list[Task], latest: dict[str, TaskRun],
          force_rerun) -> tuple[dict[str, bool], dict[str, str]]:
    """One pass producing both the reuse set and its reasons, so the plan the
    operator approves and the set the worker recomputes cannot disagree."""
    satisfied: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    for task in topological_tasks(tasks):
        row = latest.get(task.name)
        # Ordered so the reason a human would give comes first: a task that
        # broke on its own says so, and only then does the graph explain it.
        if task.name in force_rerun:
            reasons[task.name] = "you chose to"
        elif row is not None and row.status == TaskRunStatus.failed:
            reasons[task.name] = "failed"
        # Downward closure: a success that predates a re-running upstream — or a
        # dependency added since — is not evidence about the current graph.
        elif any(dep not in satisfied for dep in task.depends_on_json or []):
            reasons[task.name] = "upstream re-running"
        elif row is None or row.status != TaskRunStatus.success:
            reasons[task.name] = "did not run"  # skipped, cancelled, or gated
        else:
            satisfied[task.name] = True
    return satisfied, reasons


def satisfied_outcomes(db: Session, run: WorkflowRun, tasks: list[Task],
                       force_rerun=()) -> dict[str, bool]:
    """Task names whose success earlier in THIS run carries into the next
    execution segment; seeded into `outcomes`, they are never re-run."""
    return _walk(tasks, _latest_task_runs(db, run, tasks), force_rerun)[0]


def resume_plan(db: Session, run: WorkflowRun, tasks: list[Task], force_rerun=()) -> dict:
    """What a resume would reuse and re-run. Advisory: the worker recomputes at
    claim time, so a workflow edited in between executes the newer plan."""
    latest = _latest_task_runs(db, run, tasks)
    satisfied, reasons = _walk(tasks, latest, force_rerun)
    reuse = [{"task": name, "task_run_id": latest[name].id,
              "duration_seconds": latest[name].duration_seconds} for name in satisfied]
    return {
        "resumable": run.status in (RunStatus.failed, RunStatus.cancelled),
        "reuse": reuse,
        "rerun": [{"task": name, "reason": reason} for name, reason in reasons.items()],
        "seconds_reused": sum(item["duration_seconds"] or 0 for item in reuse),
    }


def _gate_decision(db: Session, run: WorkflowRun, task: Task) -> TaskRunStatus | None:
    """The gate's state in the run's CURRENT segment, or None when none is open.

    Scoped to the segment deliberately: resuming a rejected run must ask again
    rather than inherit the answer that cancelled it.
    """
    return db.scalar(select(TaskRun.status)
                     .where(TaskRun.workflow_run_id == run.id, TaskRun.task_id == task.id,
                            TaskRun.resume_index == run.resume_count,
                            TaskRun.status.in_(_GATE_STATUSES))
                     .order_by(TaskRun.id.desc()).limit(1))


def open_gate(db: Session, run: WorkflowRun, task: Task) -> TaskRun:
    """Park the task on a human decision, recorded as its own TaskRun."""
    gate = TaskRun(workflow_run_id=run.id, task_id=task.id, attempt=0,
                   status=TaskRunStatus.awaiting_approval, resume_index=run.resume_count)
    db.add(gate); db.commit(); db.refresh(gate)
    _ws_manager.notify({"type": "task_run_updated", "id": gate.id, "run_id": run.id})
    notify_approval_requested(db, gate)
    return gate


def _duration(start: datetime) -> float:
    current = (datetime.now(start.tzinfo) if start.tzinfo
               else datetime.now(timezone.utc).replace(tzinfo=None))
    return (current - start).total_seconds()


def _python_command(environment: Environment) -> list[str]:
    if environment.status not in (EnvironmentStatus.ready, EnvironmentStatus.degraded):
        raise RuntimeError(
            f"Environment '{environment.name}' is {environment.status.value}: "
            f"{environment.last_error or 'provisioning has not completed'}"
        )
    if environment.env_type == EnvironmentType.conda:
        conda = environment.executable or shutil.which("conda")
        if not conda or not environment.conda_env:
            raise RuntimeError(
                f"Conda environment '{environment.name}' is incomplete; configure its name and Conda executable."
            )
        return [conda, "run", "--no-capture-output", "-n", environment.conda_env, "python"]
    if environment.managed:
        python = environment_python(managed_environment_path(environment))
        if not python.is_file():
            raise RuntimeError(
                f"Managed environment '{environment.name}' is missing its Python executable; rebuild it."
            )
        return [str(python)]
    if not environment.executable:
        raise RuntimeError(f"Environment '{environment.name}' has no Python executable configured.")
    return [str(Path(environment.executable).expanduser().resolve())]


def _context(run: WorkflowRun, task_run: TaskRun, task: Task, project: Project | None,
             zone: str | None = None) -> dict:
    """The template variables. `zone` is the workflow's schedule timezone: `ds`
    is the run's date on that clock, as the schedule and the UI already read it.
    It was the UTC date, so the 18:00 Los Angeles run of Oct 17 said Oct 18
    while a backfill of Oct 17 said Oct 17 — two partitions for one day.
    `ds_utc` keeps the old value; `ts` is the exact UTC instant either way."""
    settings = get_settings()
    timestamp = run.created_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    try:
        local_day = timestamp.astimezone(ZoneInfo(zone or "UTC")).date()
    except (ZoneInfoNotFoundError, ValueError):
        local_day = timestamp.date()  # a zone the API would refuse: fall back, never fail the run
    utc_day = timestamp.date()
    # Task parameters are defaults; what a run was started with — the values
    # typed into "Run with…", or the date a backfill set — overrides them. The
    # other way round, a task that happened to define `ds` made every run of a
    # 30-day backfill process the same day.
    parameters = {**(task.parameters_json or {}), **(run.parameters_json or {})}
    if run.trigger_type == TriggerType.backfill and "ds" in parameters:
        # A backfill is about its date: `ts` names that day, not the moment the
        # range was queued, so outputs partitioned by either agree.
        try:
            timestamp = datetime.fromisoformat(str(parameters["ds"])).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return {
        "ds": parameters.get("ds", local_day.isoformat()),
        "ds_utc": parameters.get("ds", utc_day.isoformat()),
        "ts": timestamp.isoformat(),
        "ts_nodash": timestamp.strftime("%Y%m%dT%H%M%S"),
        "parameters": parameters, **parameters,
        # Last, so a parameter cannot shadow them: one named task_run_id made
        # every notebook retry write to the same output file.
        "run_id": run.id, "workflow_run_id": run.id, "task_run_id": task_run.id,
        "project_root": project.root_path if project else str(Path.cwd()),
        "artifacts_dir": str(settings.artifacts_dir.resolve() / str(run.id)),
    }


def parameter_environment(parameters: dict) -> dict[str, str]:
    """A run's parameters for a script that is not templated: all of them as
    JSON in RUNRAIL_PARAMS (types kept), and each one as RUNRAIL_PARAM_<NAME>.
    A script run by path received none of them before."""
    env = {"RUNRAIL_PARAMS": json.dumps(parameters, default=str)}
    for name, value in parameters.items():
        key = "RUNRAIL_PARAM_" + re.sub(r"[^A-Za-z0-9_]", "_", str(name)).upper()
        env[key] = ("true" if value else "false") if isinstance(value, bool) else (
            json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value))
    return env


def _python_import_paths(
    task: Task, context: dict, project: Project | None, detected_root: Path | None, cwd: Path
) -> list[Path]:
    root = Path(project.root_path).resolve() if project else (detected_root or cwd)
    candidates = [root, cwd]
    source = task.script_path or task.notebook_path
    if source:
        source_path = Path(render(source, context))
        if not source_path.is_absolute():
            source_path = cwd / source_path
        candidates.append(source_path.resolve().parent)
    src_layout = root / "src"
    if src_layout.is_dir():
        candidates.append(src_layout)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(candidate.resolve())
    return unique


def _run_task(db: Session, run: WorkflowRun, workflow: Workflow, task: Task) -> bool:
    settings = get_settings()
    project_id = task.project_id or workflow.project_id
    project = db.get(Project, project_id) if project_id else None
    environment_id = (task.environment_id or workflow.default_environment_id
                      or (project.default_environment_id if project else None))
    environment = db.get(Environment, environment_id) if environment_id else None
    # Auto-detect project root for Python/notebook tasks when no explicit project or cwd is set.
    # Walks up from the script directory looking for pyproject.toml, setup.py, .git, etc.
    detected_root: Path | None = None
    if task.task_type in (TaskType.python, TaskType.notebook) and not project and not task.cwd:
        script = task.script_path or task.notebook_path
        if script:
            detected_root = find_project_root(Path(script))
    cwd = Path(task.cwd or (project.root_path if project else (str(detected_root) if detected_root else str(Path.cwd())))).resolve()
    artifact_dir = settings.artifacts_dir.resolve() / str(run.id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # Per-run log directory: high-frequency schedules would otherwise pile
    # thousands of files into one flat directory, and retention cleanup can
    # remove a whole run's logs by deleting its directory.
    log_dir = settings.logs_dir.resolve() / f"run_{run.id}"
    log_dir.mkdir(parents=True, exist_ok=True)
    attempts = task.retries + 1
    segment = run.resume_count
    for attempt in range(1, attempts + 1):
        # Before every attempt, the first included: the pool queues every ready
        # task at once, and one waiting for a thread used to start after the
        # cancel — long enough for a deploy or a POST to complete in full.
        if _run_stopped(db, run.id, segment):
            if attempt == 1:
                db.add(TaskRun(workflow_run_id=run.id, task_id=task.id, attempt=1,
                               status=TaskRunStatus.cancelled, error_message="Run was cancelled",
                               resume_index=segment))
                db.commit()
            return False
        task_run = TaskRun(workflow_run_id=run.id, task_id=task.id, status=TaskRunStatus.running,
                           attempt=attempt, started_at=now(), resume_index=run.resume_count)
        db.add(task_run); db.commit(); db.refresh(task_run)
        stdout = log_dir / f"task_run_{task_run.id}.stdout.log"
        stderr = log_dir / f"task_run_{task_run.id}.stderr.log"
        task_run.stdout_log_path, task_run.stderr_log_path = str(stdout), str(stderr)
        db.commit()  # persist log paths so the WS streaming endpoint can find them
        _ws_manager.notify({"type": "task_run_updated", "id": task_run.id, "run_id": run.id})
        # Everything from here is inside the try: a template error in a path
        # used to escape it and leave this attempt's row `running` for good.
        try:
            context = _context(run, task_run, task, project, workflow.schedule_timezone)
            # Variables and secrets from Settings first; the environment's own
            # variables, applied next, win over them.
            env = variables.child_environment()
            configured_pythonpath = ""
            if environment and environment.env_vars_json:
                env.update({k: str(v) for k, v in environment.env_vars_json.items()})
                configured_pythonpath = str(environment.env_vars_json.get("PYTHONPATH", ""))
            if environment and environment.managed:
                environment_root = managed_environment_path(environment)
                env["PYTHONNOUSERSITE"] = "1"
                env.pop("PYTHONHOME", None)
                jupyter_path = str(environment_root / "share" / "jupyter")
                env["JUPYTER_PATH"] = jupyter_path + (
                    os.pathsep + env["JUPYTER_PATH"] if env.get("JUPYTER_PATH") else ""
                )
            runtime_executable = (
                environment_python(managed_environment_path(environment))
                if environment and environment.managed else
                Path(environment.executable).expanduser().resolve()
                if environment and environment.executable else None
            )
            if runtime_executable:
                executable_dir = str(runtime_executable.parent)
                env["PATH"] = executable_dir + os.pathsep + env.get("PATH", "")
                if (environment.env_type == EnvironmentType.python
                        and Path(executable_dir).name in {"bin", "Scripts"}):
                    env["VIRTUAL_ENV"] = str(Path(executable_dir).parent)
            # Match repository/notebook execution without recursively adding every directory,
            # which would make module resolution order-dependent and unsafe.
            import_paths = _python_import_paths(task, context, project, detected_root, cwd)
            path_parts = [*(str(path) for path in import_paths)]
            if configured_pythonpath:
                path_parts.append(configured_pythonpath)
            env["PYTHONPATH"] = os.pathsep.join(path_parts)
            env.update(parameter_environment(context["parameters"]))
            if (environment and task.task_type != TaskType.sql
                    and not environment_usable_now(environment)):
                raise RuntimeError(
                    f"Environment '{environment.name}' is not ready: "
                    f"{environment.last_error or environment.status.value}"
                )
            # What this attempt runs with, for the comparison with the last
            # good run: its scripts' versions and its packages.
            try:
                freeze_with = _python_command(environment) if environment else None
            except RuntimeError:
                freeze_with = None
            task_run.inputs_json, list_packages = capture_inputs(
                db, task, partial(render, context=context), cwd, environment, freeze_with, task_run.id)
            db.commit()
            if list_packages is not None:
                list_packages()  # after the commit, so its update lands on top of this one
            if task.task_type.value == "sql":
                task_run.rendered_command = f"sqlite3 < {render(task.sql_path, context)}"
                result = execute_sql(task, context, cwd, stdout, stderr, task.timeout_seconds,
                                     should_stop=lambda: _cancel_requested(run.id, segment),
                                     masker=variables.masker())
                artifact = None
            else:
                if task.task_type in (TaskType.python, TaskType.notebook):
                    if environment is None:
                        raise RuntimeError(
                            "No execution environment configured. Select one on the task, "
                            "workflow, or project; RunRail will not execute user code in its own environment."
                        )
                    python_command = _python_command(environment)
                else:
                    python_command = None
                module_name = None
                if task.task_type == TaskType.python and task.script_path:
                    script_path = Path(render(task.script_path, context))
                    if not script_path.is_absolute():
                        script_path = cwd / script_path
                    python_root = Path(project.root_path).resolve() if project else (detected_root or cwd)
                    module_name = python_module_name(script_path, python_root)
                spec = build_command(task, context, python_command,
                                     module_name=module_name)
                if (task.task_type == TaskType.shell and environment
                        and environment.env_type == EnvironmentType.conda):
                    command = _python_command(environment)[:-1]
                    shell = ([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
                             if os.name == "nt" else ["/bin/sh", "-c"])
                    spec = CommandSpec([*command, *shell, str(spec.command)], False, spec.artifact)
                task_run.rendered_command = variables.masker().text(display_command(spec))
                result = execute_command(spec, cwd, env, stdout, stderr, task.timeout_seconds,
                                         should_stop=lambda: _cancel_requested(run.id, segment),
                                         mask=variables.masker)
                artifact = spec.artifact
        except Exception as exc:
            hide = variables.masker().text
            stdout.touch(); stderr.write_text(hide(f"{type(exc).__name__}: {exc}\n"))
            result, artifact = type("Result", (), {"exit_code": 1, "error": hide(str(exc))})(), None
        task_run.exit_code = result.exit_code
        task_run.error_message = variables.masker().text(result.error)
        task_run.finished_at = now(); task_run.duration_seconds = _duration(task_run.started_at)
        stopped = getattr(result, "cancelled", False)
        task_run.status = (TaskRunStatus.cancelled if stopped
                           else TaskRunStatus.success if result.exit_code == 0
                           else TaskRunStatus.failed)
        if artifact and artifact.is_file():
            # papermill saved it cell by cell, outputs as printed: masked
            # before it is recorded, so no reader ever gets the raw file.
            mask_notebook(artifact, variables.masker())
            db.add(Artifact(task_run_id=task_run.id, workflow_run_id=run.id, name=artifact.name,
                            artifact_type=ArtifactType.notebook, path=str(artifact), size_bytes=artifact.stat().st_size))
        db.commit()
        _ws_manager.notify({"type": "task_run_updated", "id": task_run.id, "run_id": run.id})
        if result.exit_code == 0: return True
        # A cancelled attempt is not a failure to retry: stop here rather than
        # sleeping through the retry delay and only then noticing.
        if stopped: return False
        if attempt < attempts:
            _sleep_unless_stopped(run.id, segment, task.retry_delay_seconds)
    return False


def _sleep_unless_stopped(run_id: int, segment: int, seconds: float) -> None:
    """The retry delay, cut short by a cancel: slept whole (60s by default),
    a cancelled run's executor woke to find a resumed segment running and
    started the old segment's next attempt beside it."""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if _cancel_requested(run_id, segment):
            return
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _cancel_requested(run_id: int, segment: int | None = None) -> bool:
    """Polled once a second while a task's subprocess runs.

    On its own short-lived session, never the task's: that session holds an
    unflushed rendered_command, so querying through it would autoflush and keep
    a SQLite write transaction open for the whole task, locking out every other
    writer until the command finished.
    """
    with SessionLocal() as poll:
        return _run_stopped(poll, run_id, segment)


#: A run in one of these is over, whoever ended it: cancelled from the API, or
#: failed by another worker's recovery after this one stopped beating.
_ENDED = (RunStatus.cancelled, RunStatus.failed, RunStatus.success)


def _run_stopped(db: Session, run_id: int, segment: int | None = None) -> bool:
    """Fresh read: should work on this run stop? Yes once it has ended, and —
    given the segment this executor serves — once a resume has opened a newer
    one, which belongs to another executor.

    A run with no row left counts as stopped: deleting its workflow cascades
    the run away, and its task otherwise kept running to the end — unowned, and
    outside the resource lock the deleted row had held.
    """
    row = db.execute(select(WorkflowRun.status, WorkflowRun.resume_count)
                     .where(WorkflowRun.id == run_id)).first()
    if row is None:
        return True
    status, resume_count = row
    return status in _ENDED or (segment is not None and resume_count != segment)


def _definition(task: Task) -> dict:
    """A task's configuration as the run's graph read it."""
    return {attr.key: getattr(task, attr.key) for attr in sa_inspect(Task).column_attrs}


def _task_job(run_id: int, workflow_id: int, task_id: int,
              definition: dict | None = None) -> bool:
    """Execute one task on its own DB session (sessions are not thread-safe).

    `definition` is the task as the run's graph read it when the run started.
    The graph — order, dependencies, gates — was always that snapshot, but each
    task re-read its own row as it started, so an edit made mid-run gave the run
    the old shape with the new commands: a task rewired to depend on a new one
    ran without it, and a gate added as a brake was ignored while the command
    behind it changed. Edits now apply from the next run. The copy is never
    added to the session, so nothing is written back.
    """
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        workflow = db.get(Workflow, workflow_id)
        task = db.get(Task, task_id)
        if run is None or workflow is None or task is None:
            return False
        if definition is not None:
            task = Task(**definition)
        return _run_task(db, run, workflow, task)


def _execute_task_graph(db: Session, run: WorkflowRun, workflow: Workflow,
                        tasks: list[Task]) -> tuple[dict[str, bool], set[str]]:
    """Run the task DAG with real parallelism: a task starts the moment every
    task it depends on has succeeded, so independent tasks run concurrently.
    Tasks whose dependency failed are skipped; a cancelled run stops starting
    new tasks but lets in-flight ones finish.

    Tasks that already succeeded earlier in this run are seeded as satisfied and
    never re-run. A task waiting on an approval gate is returned in `pending`
    rather than submitted, so the caller can release the run."""
    outcomes: dict[str, bool] = satisfied_outcomes(db, run, tasks)
    # Re-entry after an approval: what this segment already decided stands. The
    # resume reuse set alone re-runs failures, so a failed task ran again on
    # every approval, doubling its retry budget per gate.
    outcomes.update(_decided_this_segment(db, run, tasks))
    remaining = {task.name: task for task in tasks if task.name not in outcomes}
    task_ids = {task.name: task.id for task in tasks}
    running: dict[Future, str] = {}
    pending: set[str] = set()
    cancelled = False
    parallelism = max(1, runtime_settings.current("task_parallelism"))
    with ThreadPoolExecutor(max_workers=parallelism,
                            thread_name_prefix=f"runrail-run-{run.id}") as pool:
        while remaining or running:
            if not cancelled:
                # Fresh session: task jobs commit on their own sessions, so polling
                # through this one could read a stale WAL snapshot forever.
                with SessionLocal() as poll_db:
                    cancelled = _run_stopped(poll_db, run.id, run.resume_count)
            ready = [task for task in list(remaining.values())
                     if all(dep in outcomes for dep in (task.depends_on_json or []))]
            for task in ready:
                del remaining[task.name]
                if cancelled:
                    db.add(TaskRun(workflow_run_id=run.id, task_id=task.id,
                                   status=TaskRunStatus.cancelled, error_message="Run was cancelled",
                                   resume_index=run.resume_count))
                    outcomes[task.name] = False; db.commit()
                elif any(not outcomes.get(dep, False) for dep in task.depends_on_json or []):
                    db.add(TaskRun(workflow_run_id=run.id, task_id=task.id,
                                   status=TaskRunStatus.skipped, error_message="A dependency did not succeed",
                                   resume_index=run.resume_count))
                    outcomes[task.name] = False; db.commit()
                elif (task.requires_approval
                      and (gate := _gate_decision(db, run, task)) != TaskRunStatus.approved):
                    if gate is None:
                        # No outcome is recorded, so downstream tasks stay in
                        # `remaining` and are picked up on re-entry.
                        open_gate(db, run, task); pending.add(task.name)
                    else:  # rejected — the existing skip cascade does the rest
                        outcomes[task.name] = False
                else:
                    running[pool.submit(_task_job, run.id, workflow.id, task.id,
                                        _definition(task))] = task.name
            if ready:
                continue  # a skip/cancel may have unlocked more tasks; re-evaluate first
            if not running:
                # An open gate legitimately leaves its downstream in `remaining`;
                # only a graph stuck with no gate is a cycle.
                if remaining and not pending:  # unreachable for a validated DAG
                    raise ValueError("Task dependency graph contains a cycle")
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                name = running.pop(future)
                error = future.exception()
                if error is not None:
                    db.add(TaskRun(workflow_run_id=run.id, task_id=task_ids[name],
                                   status=TaskRunStatus.failed, error_message=str(error),
                                   resume_index=run.resume_count))
                    db.commit()
                    outcomes[name] = False
                else:
                    outcomes[name] = bool(future.result())
    return outcomes, pending


def _decided_this_segment(db: Session, run: WorkflowRun, tasks: list[Task]) -> dict[str, bool]:
    """Each task's settled outcome in the run's current segment: True for a
    success, False for a failure, skip or cancel. Gate rows and the markers a
    forced re-run leaves at resume (attempt 0, cancelled) are not outcomes."""
    names = {task.id: task.name for task in tasks}
    decided: dict[str, bool] = {}
    for row in db.scalars(select(TaskRun).where(
            TaskRun.workflow_run_id == run.id, TaskRun.resume_index == run.resume_count,
            TaskRun.status.not_in(_GATE_STATUSES))
            .order_by(TaskRun.attempt, TaskRun.id)):
        if row.task_id not in names or (row.attempt == 0 and row.status == TaskRunStatus.cancelled):
            continue
        if row.status == TaskRunStatus.success:
            decided[names[row.task_id]] = True
        elif row.status in (TaskRunStatus.failed, TaskRunStatus.skipped, TaskRunStatus.cancelled):
            decided[names[row.task_id]] = False
    return decided


def _final_status(db: Session, run: WorkflowRun, outcomes: dict[str, bool]) -> RunStatus:
    """An empty workflow must not report success: all() of an empty dict is True."""
    if outcomes and all(outcomes.values()):
        return RunStatus.success
    statuses = set(db.scalars(select(TaskRun.status).where(
        TaskRun.workflow_run_id == run.id, TaskRun.resume_index == run.resume_count)))
    # A rejection is a decision, not a failure: landing cancelled keeps it out of
    # the failure streak and out of the alert path. A run carrying both a
    # rejection and a real failure is a failure.
    if TaskRunStatus.rejected in statuses and TaskRunStatus.failed not in statuses:
        return RunStatus.cancelled
    return RunStatus.failed


def execute_workflow_run(db: Session, run: WorkflowRun) -> None:
    workflow = db.scalar(select(Workflow).where(Workflow.id == run.workflow_id).options(selectinload(Workflow.tasks)))
    if workflow is None:
        run.status = RunStatus.failed; run.finished_at = now(); db.commit(); return
    db.commit()  # release the read snapshot; task jobs write on their own sessions
    # The segment this executor serves. A resume opens a newer one while this
    # may still be finishing; every write below is fenced to it.
    segment = run.resume_count
    final = RunStatus.failed
    pending: set[str] = set()
    try:
        tasks = topological_tasks(workflow.tasks)
        outcomes, pending = _execute_task_graph(db, run, workflow, tasks)
        final = _final_status(db, run, outcomes)
    except Exception as exc:
        final = RunStatus.failed
        # resume_index on every row a segment writes, like _run_task and
        # open_gate: a row defaulting to 0 files a resumed segment's skips and
        # failures under the original attempt, so the API reports four rows for
        # a two-task workflow and the segment filters read an incomplete set.
        db.add(TaskRun(workflow_run_id=run.id, task_id=workflow.tasks[0].id,
                       status=TaskRunStatus.failed, error_message=str(exc),
                       resume_index=run.resume_count)) if workflow.tasks else None
    if pending:
        # Release the run AND the worker slot: the pool is bounded, so a gate
        # that held a slot would deadlock the whole instance, not just this
        # workflow. Neither guarded update below may run on this path — the
        # second is guarded only on finished_at and would stamp a finish time on
        # a run that has not finished. A cancellation that raced in wins the
        # guard, and that run falls through to be finalized normally.
        parked = db.execute(update(WorkflowRun)
                            .where(WorkflowRun.id == run.id,
                                   WorkflowRun.status == RunStatus.running,
                                   WorkflowRun.resume_count == segment)
                            .values(status=RunStatus.waiting_approval)).rowcount
        if parked and not db.scalar(select(func.count()).select_from(TaskRun).where(
                TaskRun.workflow_run_id == run.id, TaskRun.resume_index == segment,
                TaskRun.status == TaskRunStatus.awaiting_approval)):
            # Every gate was decided while another branch was still running: the
            # decision found the run `running` and could not requeue it, so it
            # is requeued here, in the same transaction as the park — parked on
            # gates nobody can decide again, it held its slot and lock for good.
            db.execute(update(WorkflowRun)
                       .where(WorkflowRun.id == run.id,
                              WorkflowRun.status == RunStatus.waiting_approval)
                       .values(status=RunStatus.queued))
        db.commit()
        if parked:
            _ws_manager.notify({"type": "run_updated", "id": run.id})
            return
    if db.scalar(select(WorkflowRun.resume_count).where(WorkflowRun.id == run.id)) != segment:
        return  # resumed while this was finishing: the newer segment finalizes the run
    # Guarded update: never overwrite a cancellation that raced in from the API.
    finished = now(); duration = _duration(run.started_at or run.created_at)
    db.execute(update(WorkflowRun)
               .where(WorkflowRun.id == run.id, WorkflowRun.status == RunStatus.running,
                      WorkflowRun.resume_count == segment)
               .values(status=final, finished_at=finished, duration_seconds=duration))
    db.execute(update(WorkflowRun)
               .where(WorkflowRun.id == run.id, WorkflowRun.finished_at.is_(None),
                      WorkflowRun.resume_count == segment)
               .values(finished_at=finished, duration_seconds=duration))
    # The run is terminal on every path that reaches here, so a gate still open
    # on it is not a decision anyone can make. The one that gets here is a cancel
    # that landed between the executor's poll and the gate row it then wrote;
    # left open, that row counts against every later segment's re-entry.
    db.execute(update(TaskRun)
               .where(TaskRun.workflow_run_id == run.id,
                      TaskRun.status == TaskRunStatus.awaiting_approval)
               .values(status=TaskRunStatus.cancelled, finished_at=finished))
    db.commit()
    _ws_manager.notify({"type": "run_updated", "id": run.id})
    # Read the status that actually landed (a cancellation may have won the race).
    landed = db.scalar(select(WorkflowRun.status).where(WorkflowRun.id == run.id))
    if landed in (RunStatus.success, RunStatus.failed):
        run.status = landed
        notify_run_outcome(db, run)


def _building_environment(db: Session, run: WorkflowRun) -> Environment | None:
    """Return an environment this run needs that is still being (re)built, if any."""
    workflow = db.get(Workflow, run.workflow_id)
    if workflow is None:
        return None
    workflow_project = db.get(Project, workflow.project_id) if workflow.project_id else None
    for task in db.scalars(select(Task).where(Task.workflow_id == workflow.id)):
        task_project = db.get(Project, task.project_id) if task.project_id else workflow_project
        environment_id = (
            task.environment_id
            or workflow.default_environment_id
            or (task_project.default_environment_id if task_project else None)
        )
        if environment_id is None:
            continue
        environment = db.get(Environment, environment_id)
        if environment and environment.status in (
            EnvironmentStatus.creating, EnvironmentStatus.building
        ):
            return environment
    return None


def _over_gate_budget(db: Session, run: WorkflowRun) -> bool:
    """True when this workflow's concurrency budget is already spent once runs
    parked on an approval gate are counted.

    A backstop, not the enforcement: claim_next_run counts parked runs itself,
    so this only catches two claimers racing the same budget. Without it, a
    max_concurrent_runs=1 workflow on a schedule could send its next run straight
    past the same gate — N pending approvals and out-of-order side effects.
    """
    limit = db.scalar(select(Workflow.max_concurrent_runs)
                      .where(Workflow.id == run.workflow_id)) or 1
    active = db.scalar(select(func.count()).select_from(WorkflowRun).where(
        WorkflowRun.workflow_id == run.workflow_id,
        WorkflowRun.status.in_((RunStatus.running, RunStatus.waiting_approval)))) or 0
    return active > limit  # the run just claimed already counts as running


#: Runs passed over in one claim pass before the worker sleeps until its next poll.
_CLAIM_SKIP_LIMIT = 50


def claim_runnable_run(db: Session, owner: str | None = None) -> WorkflowRun | None:
    """Claim the next run the worker can actually start, requeueing any it
    cannot — a needed environment is mid-build, or a sibling holds the gate —
    and moving on to the next. Stopping at the first one held up every other
    workflow behind a single run waiting on a fifteen-minute pip build: it
    stays the oldest in the queue, so it was claimed and requeued every poll.
    """
    skipped: list[int] = []
    # A workflow that cannot start one run cannot start the next either (its
    # environment is building, its budget is spent): skipped whole, so fifty
    # of its backfill runs no longer hide every other workflow behind them.
    held: set[int] = set()
    while len(skipped) + len(held) < _CLAIM_SKIP_LIMIT:
        run = claim_next_run(db, skip=skipped, owner=owner, skip_workflows=held)
        if run is not None and _schedule_paused_since_queued(db, run):
            _drop(db, run)
            skipped.append(run.id)
            continue
        if run is None or not (_building_environment(db, run) or _over_gate_budget(db, run)):
            return run
        _requeue(db, run)
        held.add(run.workflow_id)
    return None


def _schedule_paused_since_queued(db: Session, run: WorkflowRun) -> bool:
    """A scheduled iteration, never started, of a workflow that has since been
    disabled, auto-paused, or snoozed with "pause scheduled runs".

    Those only stopped NEW fires. The one iteration already waiting in the
    queue still ran — so auto-pause, posting "paused after 5 consecutive
    failures", was followed by a sixth run of the broken pipeline, and a snooze
    promising "scheduled runs are skipped outright" ran one anyway. Manual runs,
    retries and resumes are someone's explicit request and still go ahead.
    """
    if run.trigger_type != TriggerType.schedule or run.resume_count:
        return False
    workflow = db.get(Workflow, run.workflow_id)
    if workflow is None:
        return False
    db.refresh(workflow)
    paused = not workflow.enabled or (workflow.snoozed and workflow.snooze_pauses_runs)
    return paused and not db.scalar(select(func.count()).select_from(TaskRun).where(
        TaskRun.workflow_run_id == run.id))


def _drop(db: Session, run: WorkflowRun) -> None:
    db.execute(update(WorkflowRun)
               .where(WorkflowRun.id == run.id, WorkflowRun.status == RunStatus.running)
               .values(status=RunStatus.cancelled, finished_at=now(), duration_seconds=0))
    _stamp_if_cancelled(db, run.id)
    db.commit()
    _ws_manager.notify({"type": "run_updated", "id": run.id})


def _requeue(db: Session, run: WorkflowRun) -> None:
    values: dict = {"status": RunStatus.queued}
    # Keep the timeline origin of a run that already executed part of this
    # segment (a gate re-entry); a run that never started must not keep the
    # timestamp this aborted claim just stamped on it.
    if not db.scalar(select(func.count()).select_from(TaskRun).where(
            TaskRun.workflow_run_id == run.id, TaskRun.resume_index == run.resume_count)):
        values["started_at"] = None
    db.execute(update(WorkflowRun)
               .where(WorkflowRun.id == run.id, WorkflowRun.status == RunStatus.running)
               .values(**values))
    _stamp_if_cancelled(db, run.id)
    db.commit()


def _stamp_if_cancelled(db: Session, run_id: int) -> None:
    """A cancel that landed between a claim and its release left the run with
    no finish time — nothing else would ever stamp one, and retention selects
    on it, so the run was kept forever."""
    db.execute(update(WorkflowRun)
               .where(WorkflowRun.id == run_id, WorkflowRun.status == RunStatus.cancelled,
                      WorkflowRun.finished_at.is_(None))
               .values(finished_at=now()))


#: Statuses a run never leaves. A `running` task row under one of these is
#: debris, not work in progress.
_TERMINAL = (RunStatus.success, RunStatus.failed, RunStatus.cancelled)


#: A worker that has not beaten for this long is taken for dead, and what it
#: was executing is recovered by whichever worker notices.
WORKER_STALE_AFTER = timedelta(seconds=90)
WORKER_BEAT_SECONDS = 10.0


class WorkerHeartbeat:
    """One worker process saying it is alive. `serve` and a separate `runrail
    worker` each have one; runs they claim carry its owner name."""

    def __init__(self, owner: str):
        self.owner = owner
        self._beaten: float | None = None

    def beat(self, at: datetime | None = None) -> None:
        with SessionLocal() as db:
            row = db.get(WorkerBeat, self.owner)
            if row is None:
                db.add(WorkerBeat(owner=self.owner, heartbeat_at=at or now()))
            else:
                row.heartbeat_at = at or now()
            # Old owners (restarted processes) are pruned as they go stale-er.
            db.execute(WorkerBeat.__table__.delete().where(
                WorkerBeat.heartbeat_at < now() - timedelta(days=1)))
            db.commit()
        self._beaten = time.monotonic()

    def due(self) -> bool:
        return self._beaten is None or time.monotonic() - self._beaten >= WORKER_BEAT_SECONDS


def _abandoned(owner: str | None):
    """Runs no live worker is executing: unclaimed by any recorded worker, or
    claimed by one that has stopped beating. Never `owner`'s own."""
    alive = select(WorkerBeat.owner).where(WorkerBeat.heartbeat_at >= now() - WORKER_STALE_AFTER)
    gone = or_(WorkflowRun.claimed_by.is_(None), WorkflowRun.claimed_by.not_in(alive))
    if owner is None:
        return gone
    return and_(gone, or_(WorkflowRun.claimed_by.is_(None), WorkflowRun.claimed_by != owner))


def recover_interrupted_runs(db: Session, owner: str | None = None) -> int:
    """Mark runs left 'running' by a killed worker as failed.

    Only runs whose worker has stopped beating: recovery used to fail every
    `running` run, so starting a second worker (`serve` beside `runrail
    worker`, which the README describes) failed the first one's live runs,
    freed their slots, and let the next run of the same workflow in beside
    them. Run at startup and every half minute after, by every worker.

    Without this, a force-quit leaves phantom running runs that permanently
    occupy their workflow's max_concurrent_runs slot — the workflow silently
    never runs again. Called on worker startup, before claiming anything.

    CONSTRAINT: a lock_resource is held by the same statuses, so this is also
    what releases a resource lock stranded by the kill. Narrowing the query
    would strand every workflow sharing that resource, not just the crashed one.
    """
    abandoned = _abandoned(owner)
    stale = db.scalars(select(WorkflowRun).where(WorkflowRun.status == RunStatus.running,
                                                 abandoned)).all()
    # A run cancelled while a task was executing keeps that task row `running`:
    # cancel settles only the queued and awaiting_approval rows, because the
    # worker is expected to finish the live one. If the worker is killed before
    # it can, nothing ever repairs the row and the run page shows a task
    # spinning forever on a run that ended. Sweep those here too, where the
    # process is known to have died.
    orphaned = db.scalars(
        select(TaskRun.workflow_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(TaskRun.status == TaskRunStatus.running,
               WorkflowRun.status.in_(_TERMINAL), abandoned)).all()
    # A cancel on a running run leaves the finish time to its worker; one
    # that died before it could left the run unresumable and kept forever.
    unstamped = db.execute(update(WorkflowRun)
                           .where(WorkflowRun.status == RunStatus.cancelled,
                                  WorkflowRun.finished_at.is_(None), abandoned)
                           .values(finished_at=now())
                           .execution_options(synchronize_session=False)).rowcount
    if not stale and not orphaned:
        if unstamped:
            db.commit()
        return 0
    finished = now()
    for run in stale:
        run.status = RunStatus.failed
        run.finished_at = finished
        run.duration_seconds = _duration(run.started_at or run.created_at)
    stale_ids = [run.id for run in stale] + list(orphaned)
    db.execute(update(TaskRun)
               .where(TaskRun.workflow_run_id.in_(stale_ids),
                      TaskRun.status == TaskRunStatus.running)
               .values(status=TaskRunStatus.failed, finished_at=finished,
                       error_message="Interrupted by worker shutdown"))
    # A gate open on a run this just failed is not a decision anyone can still
    # make, and an open gate row outlives the segment that wrote it: left alone
    # it keeps the re-entry count of every later segment above zero, so the
    # resumed run could never return to `queued`.
    db.execute(update(TaskRun)
               .where(TaskRun.workflow_run_id.in_(stale_ids),
                      TaskRun.status == TaskRunStatus.awaiting_approval)
               .values(status=TaskRunStatus.cancelled, finished_at=finished,
                       error_message="Interrupted by worker shutdown"))
    db.commit()
    # These are failures like any other: unannounced, the next success posted a
    # "recovered" nobody had seen a failure for, and the auto-pause count
    # skipped them.
    for run in stale:
        notify_run_outcome(db, run)
    return len(stale)


#: Threads the run pool is created with when Settings governs concurrency —
#: the top of the Settings range, so raising the figure never needs a restart.
_POOL_CAP = 64


class WorkerService:
    """Claims queued work and executes it on a bounded thread pool.

    Runs from different workflows execute concurrently (a one-hour job never
    blocks a five-minute schedule); runs of the same workflow are serialized by
    claim_next_run according to the workflow's max_concurrent_runs. Managed
    environment builds share the pool, so a slow pip install no longer stalls
    every workflow.

    Shutdown is two-stage: the first stop() finishes executing runs and exits;
    a second stop() (second Ctrl+C) force-quits immediately — interrupted runs
    are recovered as failed on the next start.
    """

    def __init__(self, concurrency: int | None = None):
        # Given on the command line, the figure is fixed; otherwise it is the
        # Settings value, re-read before every claim so a change applies to the
        # next run rather than the next restart. The pool itself is sized once,
        # to the most Settings allows, and the claim loop keeps to the figure.
        self._fixed_concurrency = max(1, concurrency) if concurrency else None
        self.running = True
        # This process's name on the runs it claims and on its heartbeat.
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:64]
        self.heartbeat = WorkerHeartbeat(self.owner)
        self._active: dict[Future, int | None] = {}  # future -> run id (None for env builds)
        # Services run beside the run pool, never in it: a process meant to stay
        # up would hold a worker slot for good. Its own thread; it holds a lease,
        # so with `serve` and a separate `worker` only one of them supervises.
        from runrail.services.supervisor import ServiceSupervisor
        self.supervisor = ServiceSupervisor()
        self._supervisor_thread: threading.Thread | None = None

    def stop(self, *_):
        if not self.running:  # second signal: the user really means it
            print("\nForce quitting — interrupted runs will be marked failed on the next start.",
                  flush=True)
            kill_running_tasks()
            os._exit(130)
        self.running = False
        # Services keep running until the runs have drained — a run calling
        # the model server it was written against would otherwise fail in
        # its last minutes — but nothing new is started meanwhile. run()
        # stops them once the pool is empty.
        self.supervisor.hold_starts("RunRail is stopping")
        executing = len(self.executing_run_ids())
        if executing:
            print(f"\nStopping: waiting for {executing} executing run(s) to finish. "
                  "Press Ctrl+C again to force quit.", flush=True)

    @property
    def concurrency(self) -> int:
        return self._fixed_concurrency or max(1, runtime_settings.current("worker_concurrency"))

    def executing_run_ids(self) -> list[int]:
        return [run_id for future, run_id in self._active.items()
                if run_id is not None and not future.done()]

    @staticmethod
    def _execute_run_job(run_id: int) -> None:
        with SessionLocal() as db:
            run = db.get(WorkflowRun, run_id)
            if run is not None:
                execute_workflow_run(db, run)

    def _watch_job(self, run_id: int | None):
        """A job's exception used to sit unread in its future: a run whose
        final commit failed stayed `running` until the next restart, and nobody
        was told. It is logged, and the run is failed if it is still ours."""
        def done(future: Future) -> None:
            error = future.exception()
            if error is None:
                return
            log.error("job for run %s failed", run_id, exc_info=error)
            if run_id is None:
                return
            try:
                with SessionLocal() as db:
                    db.execute(update(WorkflowRun)
                               .where(WorkflowRun.id == run_id,
                                      WorkflowRun.status == RunStatus.running,
                                      WorkflowRun.claimed_by == self.owner)
                               .values(status=RunStatus.failed, finished_at=now()))
                    db.commit()
            except Exception:
                log.exception("could not fail run %s after its job failed", run_id)
        return done

    @staticmethod
    def _provision_job(environment_id: int) -> None:
        with SessionLocal() as db:
            environment = db.get(Environment, environment_id)
            if environment is not None:
                provision_managed(db, environment)

    def _iteration(self, pool: ThreadPoolExecutor) -> bool:
        """One pass of the claim loop; True when it started something."""
        if self.heartbeat.due():
            self.heartbeat.beat()
        self._active = {future: run_id for future, run_id in self._active.items()
                        if not future.done()}
        # Paused from Settings: nothing new starts; what is executing finishes.
        if len(self._active) >= self.concurrency or runtime_settings.current("paused"):
            return False
        # Claim in a short-lived session; the job threads open their own.
        with SessionLocal() as db:
            environment = claim_next_environment(db)
            run = claim_runnable_run(db, owner=self.owner) if environment is None else None
            environment_id = environment.id if environment else None
            run_id = run.id if run else None
        if environment_id is not None:
            future = pool.submit(self._provision_job, environment_id)
            future.add_done_callback(self._watch_job(None))
            self._active[future] = None
            return True
        if run_id is not None:
            _ws_manager.notify({"type": "run_updated", "id": run_id})
            future = pool.submit(self._execute_run_job, run_id)
            future.add_done_callback(self._watch_job(run_id))
            self._active[future] = run_id
            return True
        return False

    def run(self, install_signals: bool = True) -> None:
        if install_signals:
            signal.signal(signal.SIGTERM, self.stop); signal.signal(signal.SIGINT, self.stop)
        self.heartbeat.beat()
        with SessionLocal() as db:
            recovered = recover_interrupted_runs(db, owner=self.owner)
            if recovered:
                print(f"Recovered {recovered} run(s) interrupted by a previous shutdown "
                      "(marked failed).", flush=True)
            rebuilding = queue_legacy_rebuilds(db)
            if rebuilding:
                print(f"Rebuilding {len(rebuilding)} environment(s) made by an older RunRail, "
                      f"to repair their command-line tools: {', '.join(rebuilding)}", flush=True)
        self._supervisor_thread = threading.Thread(target=self.supervisor.run,
                                                   name="runrail-services", daemon=True)
        self._supervisor_thread.start()
        pool_size = self._fixed_concurrency or max(_POOL_CAP, self.concurrency)
        with ThreadPoolExecutor(max_workers=pool_size,
                                thread_name_prefix="runrail-worker") as pool:
            recovered_at = time.monotonic()
            while self.running:
                # One bad iteration — a locked database, a dropped connection —
                # used to end the thread, and with it every run from then on,
                # while `serve` kept answering as if nothing had happened.
                try:
                    claimed = self._iteration(pool)
                    if time.monotonic() - recovered_at >= RECOVER_EVERY_SECONDS:
                        recovered_at = time.monotonic()
                        with SessionLocal() as db:
                            recover_interrupted_runs(db, owner=self.owner)
                except Exception:
                    log.exception("worker loop iteration failed; trying again shortly")
                    time.sleep(CLAIM_ERROR_BACKOFF_SECONDS)
                    continue
                if not claimed:
                    time.sleep(get_settings().worker_poll_seconds)
            # Pool context exit waits for executing runs to finish (stage one).
        self.supervisor.stop()
        if self._supervisor_thread is not None:
            # Each service gets its own stop timeout; this waits for the longest.
            self._supervisor_thread.join()
