import os
import shutil
import signal
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from runrail.api.ws import manager as _ws_manager
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.environments import (
    claim_next_environment,
    environment_python,
    managed_environment_path,
    provision_managed,
)
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
    Workflow,
    WorkflowRun,
    now,
)
from runrail.notify import notify_approval_requested, notify_run_outcome
from runrail.worker.queue import claim_next_run
from runrail.worker.runners import (
    CommandSpec,
    build_command,
    display_command,
    execute_command,
    execute_sql,
    find_project_root,
    python_module_name,
)
from runrail.worker.templating import render


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


def _context(run: WorkflowRun, task_run: TaskRun, task: Task, project: Project | None) -> dict:
    settings = get_settings()
    timestamp = run.created_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    parameters = {**(run.parameters_json or {}), **(task.parameters_json or {})}
    return {
        "ds": parameters.get("ds", timestamp.date().isoformat()), "ts": timestamp.isoformat(),
        "ts_nodash": timestamp.strftime("%Y%m%dT%H%M%S"),
        "run_id": run.id, "workflow_run_id": run.id, "task_run_id": task_run.id,
        "project_root": project.root_path if project else str(Path.cwd()),
        "artifacts_dir": str(settings.artifacts_dir.resolve() / str(run.id)),
        "parameters": parameters, **parameters,
    }


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
    for attempt in range(1, attempts + 1):
        if attempt > 1 and _run_cancelled(db, run.id):
            return False  # don't start retry attempts for a cancelled run
        task_run = TaskRun(workflow_run_id=run.id, task_id=task.id, status=TaskRunStatus.running,
                           attempt=attempt, started_at=now(), resume_index=run.resume_count)
        db.add(task_run); db.commit(); db.refresh(task_run)
        stdout = log_dir / f"task_run_{task_run.id}.stdout.log"
        stderr = log_dir / f"task_run_{task_run.id}.stderr.log"
        task_run.stdout_log_path, task_run.stderr_log_path = str(stdout), str(stderr)
        db.commit()  # persist log paths so the WS streaming endpoint can find them
        _ws_manager.notify({"type": "task_run_updated", "id": task_run.id, "run_id": run.id})
        context = _context(run, task_run, task, project)
        env = os.environ.copy()
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
        try:
            if environment and environment.status not in (
                EnvironmentStatus.ready, EnvironmentStatus.degraded
            ):
                raise RuntimeError(
                    f"Environment '{environment.name}' is not ready: "
                    f"{environment.last_error or environment.status.value}"
                )
            if task.task_type.value == "sql":
                task_run.rendered_command = f"sqlite3 < {render(task.sql_path, context)}"
                result = execute_sql(task, context, cwd, stdout, stderr)
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
                task_run.rendered_command = display_command(spec)
                result = execute_command(spec, cwd, env, stdout, stderr, task.timeout_seconds)
                artifact = spec.artifact
        except Exception as exc:
            stdout.touch(); stderr.write_text(f"{type(exc).__name__}: {exc}\n")
            result, artifact = type("Result", (), {"exit_code": 1, "error": str(exc)})(), None
        task_run.exit_code = result.exit_code
        task_run.error_message = result.error
        task_run.finished_at = now(); task_run.duration_seconds = _duration(task_run.started_at)
        task_run.status = TaskRunStatus.success if result.exit_code == 0 else TaskRunStatus.failed
        if artifact and artifact.is_file():
            db.add(Artifact(task_run_id=task_run.id, workflow_run_id=run.id, name=artifact.name,
                            artifact_type=ArtifactType.notebook, path=str(artifact), size_bytes=artifact.stat().st_size))
        db.commit()
        _ws_manager.notify({"type": "task_run_updated", "id": task_run.id, "run_id": run.id})
        if result.exit_code == 0: return True
        if attempt < attempts: time.sleep(task.retry_delay_seconds)
    return False


def _run_cancelled(db: Session, run_id: int) -> bool:
    """Fresh read of the run status so API-side cancellations are honoured mid-run."""
    return db.scalar(select(WorkflowRun.status).where(WorkflowRun.id == run_id)) == RunStatus.cancelled


def _task_job(run_id: int, workflow_id: int, task_id: int) -> bool:
    """Execute one task on its own DB session (sessions are not thread-safe)."""
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        workflow = db.get(Workflow, workflow_id)
        task = db.get(Task, task_id)
        if run is None or workflow is None or task is None:
            return False
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
    remaining = {task.name: task for task in tasks if task.name not in outcomes}
    task_ids = {task.name: task.id for task in tasks}
    running: dict[Future, str] = {}
    pending: set[str] = set()
    cancelled = False
    parallelism = max(1, get_settings().task_parallelism)
    with ThreadPoolExecutor(max_workers=parallelism,
                            thread_name_prefix=f"runrail-run-{run.id}") as pool:
        while remaining or running:
            if not cancelled:
                # Fresh session: task jobs commit on their own sessions, so polling
                # through this one could read a stale WAL snapshot forever.
                with SessionLocal() as poll_db:
                    cancelled = _run_cancelled(poll_db, run.id)
            ready = [task for task in list(remaining.values())
                     if all(dep in outcomes for dep in (task.depends_on_json or []))]
            for task in ready:
                del remaining[task.name]
                if cancelled:
                    db.add(TaskRun(workflow_run_id=run.id, task_id=task.id,
                                   status=TaskRunStatus.cancelled, error_message="Run was cancelled"))
                    outcomes[task.name] = False; db.commit()
                elif any(not outcomes.get(dep, False) for dep in task.depends_on_json or []):
                    db.add(TaskRun(workflow_run_id=run.id, task_id=task.id,
                                   status=TaskRunStatus.skipped, error_message="A dependency did not succeed"))
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
                    running[pool.submit(_task_job, run.id, workflow.id, task.id)] = task.name
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
                                   status=TaskRunStatus.failed, error_message=str(error)))
                    db.commit()
                    outcomes[name] = False
                else:
                    outcomes[name] = bool(future.result())
    return outcomes, pending


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
    final = RunStatus.failed
    pending: set[str] = set()
    try:
        tasks = topological_tasks(workflow.tasks)
        outcomes, pending = _execute_task_graph(db, run, workflow, tasks)
        final = _final_status(db, run, outcomes)
    except Exception as exc:
        final = RunStatus.failed
        db.add(TaskRun(workflow_run_id=run.id, task_id=workflow.tasks[0].id,
                       status=TaskRunStatus.failed, error_message=str(exc))) if workflow.tasks else None
    if pending:
        # Release the run AND the worker slot: the pool is bounded, so a gate
        # that held a slot would deadlock the whole instance, not just this
        # workflow. Neither guarded update below may run on this path — the
        # second is guarded only on finished_at and would stamp a finish time on
        # a run that has not finished. A cancellation that raced in wins the
        # guard, and that run falls through to be finalized normally.
        parked = db.execute(update(WorkflowRun)
                            .where(WorkflowRun.id == run.id,
                                   WorkflowRun.status == RunStatus.running)
                            .values(status=RunStatus.waiting_approval)).rowcount
        db.commit()
        if parked:
            _ws_manager.notify({"type": "run_updated", "id": run.id})
            return
    # Guarded update: never overwrite a cancellation that raced in from the API.
    finished = now(); duration = _duration(run.started_at or run.created_at)
    db.execute(update(WorkflowRun)
               .where(WorkflowRun.id == run.id, WorkflowRun.status == RunStatus.running)
               .values(status=final, finished_at=finished, duration_seconds=duration))
    db.execute(update(WorkflowRun)
               .where(WorkflowRun.id == run.id, WorkflowRun.finished_at.is_(None))
               .values(finished_at=finished, duration_seconds=duration))
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

    CONSTRAINT: claim_next_run counts only `running` runs, so a waiting run does
    not hold its workflow's slot there. Without this re-check, a
    max_concurrent_runs=1 workflow on a schedule sends its next run straight
    past the same gate — N pending approvals and out-of-order side effects.
    """
    limit = db.scalar(select(Workflow.max_concurrent_runs)
                      .where(Workflow.id == run.workflow_id)) or 1
    active = db.scalar(select(func.count()).select_from(WorkflowRun).where(
        WorkflowRun.workflow_id == run.workflow_id,
        WorkflowRun.status.in_((RunStatus.running, RunStatus.waiting_approval)))) or 0
    return active > limit  # the run just claimed already counts as running


def claim_runnable_run(db: Session) -> WorkflowRun | None:
    """Claim the next run the worker can actually start, requeueing one it
    cannot: a needed environment is mid-build, or a sibling holds the gate."""
    run = claim_next_run(db)
    if run is None or not (_building_environment(db, run) or _over_gate_budget(db, run)):
        return run
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
    db.commit()
    return None


def recover_interrupted_runs(db: Session) -> int:
    """Mark runs left 'running' by a killed worker as failed.

    Without this, a force-quit leaves phantom running runs that permanently
    occupy their workflow's max_concurrent_runs slot — the workflow silently
    never runs again. Called on worker startup, before claiming anything.
    """
    stale = db.scalars(select(WorkflowRun).where(WorkflowRun.status == RunStatus.running)).all()
    if not stale:
        return 0
    finished = now()
    for run in stale:
        run.status = RunStatus.failed
        run.finished_at = finished
        run.duration_seconds = _duration(run.started_at or run.created_at)
    db.execute(update(TaskRun)
               .where(TaskRun.workflow_run_id.in_([run.id for run in stale]),
                      TaskRun.status == TaskRunStatus.running)
               .values(status=TaskRunStatus.failed, finished_at=finished,
                       error_message="Interrupted by worker shutdown"))
    db.commit()
    return len(stale)


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
        self.concurrency = max(1, concurrency or get_settings().worker_concurrency)
        self.running = True
        self._active: dict[Future, int | None] = {}  # future -> run id (None for env builds)

    def stop(self, *_):
        if not self.running:  # second signal: the user really means it
            print("\nForce quitting — interrupted runs will be marked failed on the next start.",
                  flush=True)
            os._exit(130)
        self.running = False
        executing = len(self.executing_run_ids())
        if executing:
            print(f"\nStopping: waiting for {executing} executing run(s) to finish. "
                  "Press Ctrl+C again to force quit.", flush=True)

    def executing_run_ids(self) -> list[int]:
        return [run_id for future, run_id in self._active.items()
                if run_id is not None and not future.done()]

    @staticmethod
    def _execute_run_job(run_id: int) -> None:
        with SessionLocal() as db:
            run = db.get(WorkflowRun, run_id)
            if run is not None:
                execute_workflow_run(db, run)

    @staticmethod
    def _provision_job(environment_id: int) -> None:
        with SessionLocal() as db:
            environment = db.get(Environment, environment_id)
            if environment is not None:
                provision_managed(db, environment)

    def run(self, install_signals: bool = True) -> None:
        if install_signals:
            signal.signal(signal.SIGTERM, self.stop); signal.signal(signal.SIGINT, self.stop)
        with SessionLocal() as db:
            recovered = recover_interrupted_runs(db)
            if recovered:
                print(f"Recovered {recovered} run(s) interrupted by a previous shutdown "
                      "(marked failed).", flush=True)
        with ThreadPoolExecutor(max_workers=self.concurrency,
                                thread_name_prefix="runrail-worker") as pool:
            while self.running:
                self._active = {future: run_id for future, run_id in self._active.items()
                                if not future.done()}
                claimed = False
                if len(self._active) < self.concurrency:
                    # Claim in a short-lived session; the job threads open their own.
                    with SessionLocal() as db:
                        environment = claim_next_environment(db)
                        run = claim_runnable_run(db) if environment is None else None
                        environment_id = environment.id if environment else None
                        run_id = run.id if run else None
                    if environment_id is not None:
                        self._active[pool.submit(self._provision_job, environment_id)] = None
                        claimed = True
                    elif run_id is not None:
                        _ws_manager.notify({"type": "run_updated", "id": run_id})
                        self._active[pool.submit(self._execute_run_job, run_id)] = run_id
                        claimed = True
                if not claimed:
                    time.sleep(get_settings().worker_poll_seconds)
            # Pool context exit waits for executing runs to finish (stage one).
