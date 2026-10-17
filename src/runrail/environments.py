import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from runrail.api.ws import manager as _ws_manager
from runrail.config import get_settings
from runrail.models import (
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    Project,
    RunStatus,
    Service,
    Task,
    Workflow,
    WorkflowRun,
    now,
)

_LOG_LIMIT = 100_000
_RUNTIME_PACKAGES = ("papermill>=2.6,<3", "ipykernel>=6,<8")


def managed_environment_path(environment: Environment) -> Path:
    if environment.id is None:
        raise ValueError("Environment must be saved before it can be provisioned")
    root = get_settings().environments_dir.resolve()
    if environment.executable:
        # The directory, resolved — never the interpreter: a venv's python is a
        # symlink to the base install, so resolving it lands outside the venv.
        candidate = Path(environment.executable).expanduser().absolute().parent.parent.resolve()
        if candidate.parent == root:
            return candidate
    return (root / f"env-{environment.id}").resolve()


def environment_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def validate_package_specs(packages: list[str]) -> list[str]:
    cleaned = []
    for package in packages:
        value = package.strip()
        if not value:
            continue
        if value.startswith("-") or "\n" in value or "\r" in value:
            raise ValueError(f"Invalid package requirement: {package!r}")
        cleaned.append(value)
    return cleaned


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False, env=environment,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )


def validate_python(executable: str) -> tuple[str, str]:
    python = Path(executable).expanduser().resolve()
    if not python.is_file():
        raise ValueError(f"Python executable does not exist: {python}")
    try:
        check = _run(
            [str(python), "-c", "import platform,sys;print(sys.executable);print(platform.python_version())"],
            20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not run Python executable: {exc}") from exc
    if check.returncode:
        detail = check.stderr.strip() or f"exit code {check.returncode}"
        raise ValueError(f"Executable is not a working Python interpreter: {detail}")
    lines = check.stdout.strip().splitlines()
    return str(python), lines[-1] if lines else "unknown"


def validate_conda(executable: str | None, name: str | None) -> tuple[str, str]:
    conda = Path(executable).expanduser().resolve() if executable else None
    if conda is None:
        found = shutil.which("conda")
        conda = Path(found).resolve() if found else None
    if conda is None or not conda.is_file():
        raise ValueError("Conda executable was not found")
    if not name:
        raise ValueError("Conda environment name is required")
    try:
        check = _run(
            [str(conda), "run", "-n", name, "python", "-c", "import platform;print(platform.python_version())"],
            60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not validate Conda environment: {exc}") from exc
    if check.returncode:
        raise ValueError(f"Conda environment '{name}' is unavailable: {check.stderr.strip()}")
    return str(conda), check.stdout.strip().splitlines()[-1]


def validate_external(environment: Environment) -> None:
    if environment.env_type == EnvironmentType.conda:
        environment.executable, environment.python_version = validate_conda(
            environment.executable, environment.conda_env
        )
    else:
        if not environment.executable:
            raise ValueError("Python executable is required")
        environment.executable, environment.python_version = validate_python(environment.executable)
    environment.status = EnvironmentStatus.ready
    environment.last_error = None


#: How often a build refreshes its claim on the environment, and how long a
#: claim may go unrefreshed before it counts as abandoned — by a process killed
#: mid-pip, say — and the build is handed out again. Without the heartbeat the
#: second number had to exceed the longest possible build (thirty minutes), so
#: an interrupted build kept the environment locked against edits for that long,
#: and a build that really did run longer was given to a second thread anyway.
BUILD_HEARTBEAT_SECONDS = 30.0
BUILD_STALE_AFTER = timedelta(minutes=3)


class _ClaimHeartbeat:
    """Refreshes build_started_at while a build runs, from its own session."""

    def __init__(self, environment_id: int):
        self._id = environment_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True,
                                        name=f"runrail-build-{environment_id}")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=5)

    def _beat(self) -> None:
        from runrail.db import SessionLocal
        while not self._stop.wait(BUILD_HEARTBEAT_SECONDS):
            try:
                with SessionLocal() as db:
                    db.execute(update(Environment).where(
                        Environment.id == self._id,
                        Environment.status == EnvironmentStatus.building,
                    ).values(build_started_at=now()))
                    db.commit()
            except Exception:
                pass  # a missed beat is retried; the threshold allows for several


def _environment_directories(environment_id: int) -> list[Path]:
    """Every directory under the environments root that belongs to one
    environment: its current build, older builds, and the staging and backup
    directories earlier versions of RunRail made."""
    root = get_settings().environments_dir.resolve()
    if not root.is_dir():
        return []
    name = f"env-{environment_id}"
    return [path for path in root.iterdir()
            if path.name == name or path.name.startswith((f"{name}-", f".{name}."))]


def _sweep_directories(environment_id: int, keep: Path | None) -> None:
    for path in _environment_directories(environment_id):
        if keep is None or path.resolve() != keep.resolve():
            shutil.rmtree(path, ignore_errors=True)


def provision_managed(db: Session, environment: Environment) -> Environment:
    """Build a managed environment, or rebuild it, in a directory of its own.

    Each build goes straight into a new directory that it never leaves, and the
    environment switches to it only once it works. Earlier builds were made in
    a staging directory and renamed into place, but a venv cannot be moved:
    every console script pip installs — pytest, dbt, papermill, the user's own
    CLIs — hard-codes the interpreter path it was installed with, so after the
    rename each one failed with "bad interpreter". The rename was also the step
    Windows refuses while antivirus or a sync client holds a file, and a crash
    between the two renames of a rebuild lost the working environment.
    """
    if not environment.managed:
        raise ValueError("Only managed environments can be provisioned")
    if environment.id is None:
        raise ValueError("Environment must be saved before it can be provisioned")
    root = get_settings().environments_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    previous = (environment_python(managed_environment_path(environment))
                if environment.executable else None)
    had_working_environment = previous is not None and previous.is_file()
    target = root / f"env-{environment.id}-{uuid.uuid4().hex[:8]}"
    log_parts: list[str] = []
    built = False
    with _ClaimHeartbeat(environment.id):
        try:
            packages = validate_package_specs(environment.packages_json or [])
            base = environment.base_executable or getattr(sys, "_base_executable", sys.executable)
            base, _ = validate_python(base)
            environment.status = EnvironmentStatus.building
            environment.last_error = None
            environment.build_log = None
            environment.base_executable = base
            environment.packages_json = packages
            db.commit()
            created = _run([base, "-m", "venv", str(target)], 180)
            log_parts.extend(filter(None, [created.stdout, created.stderr]))
            if created.returncode:
                raise RuntimeError(f"venv creation exited with code {created.returncode}")
            python = environment_python(target)
            install_packages = [*_RUNTIME_PACKAGES, *packages]
            if install_packages:
                command = [
                    str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-input", *install_packages,
                ]
                completed = _run(command, 900)
                log_parts.extend(filter(None, [completed.stdout, completed.stderr]))
                if completed.returncode:
                    raise RuntimeError(f"pip exited with code {completed.returncode}")
            if _RUNTIME_PACKAGES:
                runtime_check = _run(
                    [str(python), "-c", "import ipykernel, papermill; print('notebook runtime ready')"],
                    60,
                )
                log_parts.extend(filter(None, [runtime_check.stdout, runtime_check.stderr]))
                if runtime_check.returncode:
                    raise RuntimeError("managed notebook runtime import check failed")
            _, version = validate_python(str(python))
            environment.executable = str(python)
            environment.python_version = version
            environment.status = EnvironmentStatus.ready
            environment.active_packages_json = packages
            environment.last_built_at = now()
            environment.last_error = None
            built = True
        except Exception as exc:
            # The previous build was never touched, so a working one still works.
            environment.status = (
                EnvironmentStatus.degraded if had_working_environment else EnvironmentStatus.failed
            )
            environment.last_error = str(exc)
            log_parts.append(f"{type(exc).__name__}: {exc}")
        finally:
            environment.build_started_at = None
            environment.build_log = "\n".join(log_parts)[-_LOG_LIMIT:] or None
            # Commit before deleting anything, so the row never points at a
            # directory that is already gone.
            db.commit()
            keep = target if built else (previous.parents[1] if had_working_environment else None)
            _sweep_directories(environment.id, keep)
            db.refresh(environment)
            _ws_manager.notify({"type": "environment_updated", "id": environment.id})
    return environment


def environment_ids_in_use(db: Session) -> set[int]:
    """Environments resolvable by any task of a currently running workflow run.

    Resolution mirrors the worker: task override → workflow default → project default.
    """
    in_use: set[int] = set()
    running = db.scalars(
        select(WorkflowRun).where(WorkflowRun.status == RunStatus.running)
    ).all()
    for run in running:
        workflow = db.get(Workflow, run.workflow_id)
        if workflow is None:
            continue
        workflow_project = db.get(Project, workflow.project_id) if workflow.project_id else None
        for task in db.scalars(select(Task).where(Task.workflow_id == workflow.id)):
            task_project = db.get(Project, task.project_id) if task.project_id else workflow_project
            environment_id = (
                task.environment_id
                or workflow.default_environment_id
                or (task_project.default_environment_id if task_project else None)
            )
            if environment_id is not None:
                in_use.add(environment_id)
    # A service holding its process runs from its environment too: rebuilding
    # would swap the venv out from under it, so a rebuild waits for it to stop.
    from runrail.services.locks import HOLDING
    in_use.update(db.scalars(select(Service.environment_id).where(
        Service.environment_id.is_not(None), Service.state.in_(HOLDING))))
    return in_use


def environment_usable_now(environment: Environment) -> bool:
    """Whether a task may execute in this environment at this moment.

    A rebuild that is requested but not yet started (`creating` with a built
    interpreter) leaves the current venv intact, and the build itself waits
    until no running run uses the environment — so the run already using it
    carries on there. Refusing it instead failed every remaining task of that
    run, after which the deferred build went ahead anyway.
    """
    if environment.status in (EnvironmentStatus.ready, EnvironmentStatus.degraded):
        return True
    return (environment.status == EnvironmentStatus.creating and environment.managed
            and bool(environment.executable) and Path(environment.executable).is_file())


def queue_legacy_rebuilds(db: Session) -> list[str]:
    """Queue a rebuild of every managed environment still in the layout before
    0.6.0, and return their names.

    Those were built in a staging directory and renamed to env-<id>, which left
    every console script they contain — pytest, dbt, papermill, the user's own
    CLIs — pointing at an interpreter path that no longer exists. Only a rebuild
    repairs them. It waits for runs using the environment like any rebuild, and
    tasks keep running on the current venv until it starts; if it fails
    (offline, a package withdrawn) the environment is marked degraded and keeps
    working exactly as before, and the next start tries again.
    """
    root = get_settings().environments_dir.resolve()
    queued = []
    for environment in db.scalars(select(Environment).where(
            Environment.managed.is_(True), Environment.executable.is_not(None),
            Environment.status.in_((EnvironmentStatus.ready, EnvironmentStatus.degraded)))):
        if managed_environment_path(environment) != root / f"env-{environment.id}":
            continue
        claimed = db.execute(update(Environment).where(
            Environment.id == environment.id,
            Environment.status.in_((EnvironmentStatus.ready, EnvironmentStatus.degraded)),
        ).values(status=EnvironmentStatus.creating, last_error=None))
        if claimed.rowcount == 1:
            queued.append(environment.name)
    db.commit()
    return queued


def claim_next_environment(db: Session) -> Environment | None:
    stale_before = now() - BUILD_STALE_AFTER
    db.execute(
        update(Environment)
        .where(
            Environment.status == EnvironmentStatus.building,
            or_(
                Environment.build_started_at.is_(None),
                Environment.build_started_at < stale_before,
            ),
        )
        .values(status=EnvironmentStatus.creating, build_started_at=None)
    )
    db.commit()
    # Never rebuild an environment while a running task may be executing from it:
    # the atomic swap would delete site-packages out from under the live subprocess.
    busy = environment_ids_in_use(db)
    pending = select(Environment.id).where(
        Environment.managed.is_(True), Environment.status == EnvironmentStatus.creating
    )
    if busy:
        pending = pending.where(Environment.id.not_in(busy))
    environment_id = db.scalar(pending.order_by(Environment.updated_at, Environment.id).limit(1))
    if environment_id is None:
        return None
    claimed = db.execute(
        update(Environment)
        .where(
            Environment.id == environment_id,
            Environment.status == EnvironmentStatus.creating,
        )
        .values(status=EnvironmentStatus.building, build_started_at=now())
    )
    db.commit()
    return db.get(Environment, environment_id) if claimed.rowcount == 1 else None


def remove_managed_files(environment_id: int) -> None:
    """Delete everything a managed environment has on disk. Only directories
    directly under the environments root, named for this environment."""
    _sweep_directories(environment_id, keep=None)
