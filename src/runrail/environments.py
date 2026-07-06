import os
import shutil
import subprocess
import sys
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
        executable = Path(environment.executable).expanduser().resolve()
        candidate = executable.parent.parent
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


def provision_managed(db: Session, environment: Environment) -> Environment:
    if not environment.managed:
        raise ValueError("Only managed environments can be provisioned")
    target = managed_environment_path(environment)
    staging = target.with_name(f".{target.name}.build-{uuid.uuid4().hex[:8]}")
    had_working_environment = target.is_dir() and bool(environment.executable)
    log_parts: list[str] = []
    backup: Path | None = None
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
        created = _run([base, "-m", "venv", str(staging)], 180)
        log_parts.extend(filter(None, [created.stdout, created.stderr]))
        if created.returncode:
            raise RuntimeError(f"venv creation exited with code {created.returncode}")
        python = environment_python(staging)
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
        if target.exists():
            backup = target.with_name(f".{target.name}.previous-{uuid.uuid4().hex[:8]}")
            target.rename(backup)
        staging.rename(target)
        # pip's shebang still points to the staging path after the rename; reinstalling
        # pip via the venv's own python rewrites the script with the correct path.
        pip_fixup = _run(
            [str(environment_python(target)), "-m", "pip", "install",
             "--force-reinstall", "--quiet", "--disable-pip-version-check", "--no-input", "pip"],
            120,
        )
        log_parts.extend(filter(None, [pip_fixup.stdout, pip_fixup.stderr]))
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
        environment.executable = str(environment_python(target))
        environment.python_version = version
        environment.status = EnvironmentStatus.ready
        environment.active_packages_json = packages
        environment.last_built_at = now()
        environment.build_started_at = None
        environment.last_error = None
    except Exception as exc:
        if backup and backup.exists() and not target.exists():
            backup.rename(target)
        environment.status = (
            EnvironmentStatus.degraded if had_working_environment else EnvironmentStatus.failed
        )
        environment.build_started_at = None
        environment.last_error = str(exc)
        log_parts.append(f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        environment.build_log = "\n".join(log_parts)[-_LOG_LIMIT:] or None
        db.commit()
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
    return in_use


def claim_next_environment(db: Session) -> Environment | None:
    stale_before = now() - timedelta(minutes=30)
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


def remove_managed_root(target: Path) -> None:
    """Delete a managed environment directory, refusing paths outside the environments root."""
    root = get_settings().environments_dir.resolve()
    if target.parent == root:
        shutil.rmtree(target, ignore_errors=True)


def remove_managed_files(environment: Environment) -> None:
    if not environment.managed:
        return
    remove_managed_root(managed_environment_path(environment))
