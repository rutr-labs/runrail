"""Launching, signalling and logging a service's process.

A service is a shell command, run the way a shell task is: in its project's
folder (or its own cwd), with its environment's interpreter first on PATH, so
`python sync.py` or `llama-server -m model.gguf` resolve inside the chosen
environment. The process gets a process group of its own, so a stop reaches
whatever the shell started, not just the shell.
"""

import os
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import psutil
from sqlalchemy.orm import Session

from runrail.config import get_settings
from runrail.environments import (
    environment_python,
    environment_usable_now,
    managed_environment_path,
)
from runrail.models import Environment, EnvironmentType, Project, Service

#: Size at which a service's log rolls over, and how many old files are kept.
#: A service writes for weeks; a task-style single file would fill the disk.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3


@dataclass
class LaunchSpec:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    display: str


def _shell(command: str) -> list[str]:
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/sh", "-c", command]


def launch_spec(db: Session, service: Service) -> LaunchSpec:
    """How to start this service now, or RuntimeError saying why it cannot."""
    settings = get_settings()
    project = db.get(Project, service.project_id) if service.project_id else None
    environment = db.get(Environment, service.environment_id) if service.environment_id else None
    env = os.environ.copy()
    # Output reaches the log as it is written, not when a buffer fills.
    env["PYTHONUNBUFFERED"] = "1"
    argv = _shell(service.command)
    if environment is not None:
        if not environment_usable_now(environment):
            raise RuntimeError(f"Environment '{environment.name}' is not ready: "
                               f"{environment.last_error or environment.status.value}")
        env.update({key: str(value) for key, value in (environment.env_vars_json or {}).items()})
        if environment.env_type == EnvironmentType.conda:
            conda = environment.executable or shutil.which("conda")
            if not conda or not environment.conda_env:
                raise RuntimeError(f"Conda environment '{environment.name}' is incomplete")
            argv = [conda, "run", "--no-capture-output", "-n", environment.conda_env, *argv]
        else:
            python = (environment_python(managed_environment_path(environment))
                      if environment.managed else
                      Path(environment.executable).expanduser() if environment.executable else None)
            if python is not None:
                env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
                if python.parent.name in {"bin", "Scripts"}:
                    env["VIRTUAL_ENV"] = str(python.parent.parent)
                    env.pop("PYTHONHOME", None)
    env.update({key: str(value) for key, value in (service.env_vars_json or {}).items()})
    cwd = Path(service.cwd or (project.root_path if project else settings.home)).expanduser()
    if not cwd.is_dir():
        raise RuntimeError(f"Working directory does not exist: {cwd}")
    return LaunchSpec(argv, cwd, env, service.command)


def log_directory(service_id: int) -> Path:
    return get_settings().logs_dir.resolve() / "services" / str(service_id)


def start(spec: LaunchSpec, log_path: Path) -> tuple[subprocess.Popen, threading.Thread]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        spec.argv, cwd=spec.cwd, env=spec.env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=os.name != "nt",
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    pump = threading.Thread(target=_pump, args=(process.stdout, log_path), daemon=True,
                            name=f"runrail-service-log-{process.pid}")
    pump.start()
    return process, pump


def _pump(stream, path: Path) -> None:
    """Copy a process's output to its log, rolling the file over by size."""
    out = path.open("ab")
    written = out.tell()
    try:
        for chunk in iter(lambda: stream.read1(65536), b""):
            if written + len(chunk) > LOG_MAX_BYTES and written:
                out.close()
                _rotate(path)
                out = path.open("ab")
                written = 0
            out.write(chunk)
            out.flush()
            written += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        out.close()


def _rotate(path: Path) -> None:
    for index in range(LOG_BACKUPS, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index - 1}") if index > 1 else path
        if newer.exists():
            try:
                os.replace(newer, older)
            except OSError:
                pass


def note(log_path: Path, message: str) -> None:
    """A line from RunRail itself in a service's log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[runrail] {message}\n")


def created(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def same_process(pid: int | None, created_at: float | None) -> bool:
    """Whether `pid` still names the process recorded at `created_at`. A pid is
    never signalled on the strength of its number alone: after a crash it may
    belong to something else entirely."""
    if not pid or created_at is None:
        return False
    try:
        process = psutil.Process(pid)
        return (abs(process.create_time() - created_at) < 1.0
                and process.status() != psutil.STATUS_ZOMBIE)
    except (psutil.Error, OSError):
        return False


def group_alive(pid: int) -> bool:
    """Whether anything in the process's group is still running (POSIX), or
    the process itself (Windows, where the tree is killed whole)."""
    if os.name == "nt":
        try:
            return psutil.Process(pid).is_running()
        except (psutil.Error, OSError):
            return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ask_to_stop(pid: int) -> None:
    """The polite stop: SIGTERM to the group, or Ctrl+Break on Windows (the
    process was started in a group of its own, which is what lets it receive
    one). What the program does with it is up to the program — a model server
    finishes the request it is serving — hence the timeout behind it."""
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def kill(pid: int) -> None:
    """The forced stop, for the whole group or tree."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False, timeout=15)
        else:
            os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
