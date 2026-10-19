import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runrail.masking import Masker, StreamMasker
from runrail.models import Task, TaskType
from runrail.worker.templating import render

_ROOT_MARKERS = frozenset({"pyproject.toml", "setup.py", "setup.cfg", ".git", ".hg"})


def safe_filename(value: str, fallback: str = "artifact") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._")
    return cleaned[:180] or fallback


def find_project_root(script_path: Path) -> Path | None:
    """Walk up from the script's directory to find the nearest project root marker."""
    directory = script_path.resolve().parent
    while True:
        if any((directory / m).exists() for m in _ROOT_MARKERS):
            return directory
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


@dataclass
class CommandSpec:
    command: str | list[str]
    shell: bool
    artifact: Path | None = None


@dataclass
class ExecutionResult:
    exit_code: int
    error: str | None = None
    #: Stopped because the run was cancelled, as opposed to failing on its own.
    cancelled: bool = False


#: How often a running subprocess is checked for a timeout or a cancellation.
#: Also the worst-case delay between pressing Cancel and the process dying.
POLL_SECONDS = 1.0


def python_module_name(script_path: Path, project_root: Path) -> str | None:
    """Return an importable module name when *script_path* is inside a package."""
    try:
        relative = script_path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    if relative.suffix != ".py" or not relative.parent.parts:
        return None
    if not all((project_root / Path(*relative.parts[:index]) / "__init__.py").is_file()
               for index in range(1, len(relative.parts))):
        return None
    return ".".join(relative.with_suffix("").parts)


def build_command(task: Task, context: dict[str, Any], python_command: list[str] | None = None,
                  module_name: str | None = None) -> CommandSpec:
    if task.task_type == TaskType.shell:
        if not task.command: raise ValueError("Shell task requires a command")
        return CommandSpec(render(task.command, context), True)
    if task.task_type == TaskType.python:
        if task.command: return CommandSpec(render(task.command, context), True)
        if not task.script_path: raise ValueError("Python task requires script_path or command")
        python = python_command or [sys.executable]
        if module_name:
            return CommandSpec([*python, "-m", module_name], False)
        return CommandSpec([*python, render(task.script_path, context)], False)
    if task.task_type == TaskType.notebook:
        if not task.notebook_path: raise ValueError("Notebook task requires notebook_path")
        # Stamp outputs with the run timestamp (not just the date) plus the task-run
        # id: high-frequency schedules produce many runs per day, and retry attempts
        # of the same run must not overwrite each other's output.
        stamp = safe_filename(str(context.get("ts_nodash") or context.get("ds") or "run"), "run")
        task_run_id = context.get("task_run_id")
        suffix = f"_{task_run_id}" if task_run_id is not None else ""
        output = Path(context["artifacts_dir"]) / (
            f"{safe_filename(task.name, 'notebook')}_{stamp}{suffix}.ipynb"
        )
        # nbexec (from RunRail's own install) wraps papermill so the kernel
        # runs on IPC transport instead of unencrypted loopback TCP.
        command = [*(python_command or [sys.executable]),
                   str(Path(__file__).with_name("nbexec.py")),
                   render(task.notebook_path, context), str(output), "--kernel", "python3"]
        for key, value in context.get("parameters", {}).items(): command += ["-p", key, str(value)]
        return CommandSpec(command, False, output)
    raise ValueError(f"Task type {task.task_type.value} is handled separately")


def _terminate(process: subprocess.Popen) -> None:
    """Stop the whole process group: the command, and anything it spawned.

    A shell task is `sh -c "..."` and a notebook is papermill plus a kernel, so
    killing only the direct child would leave the real work running.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        group = process.pid  # start_new_session: the leader's pid is the group id
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Wait for the GROUP, not the leader. `sh -c "python etl.py; ..."` dies
        # on SIGTERM at once while a python that handles the signal carries on;
        # waiting on the shell alone skipped the SIGKILL, and the retry started
        # beside the attempt it was meant to replace.
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            process.poll()  # reap the leader so it does not linger as a zombie
            if not _group_alive(group):
                break
            time.sleep(0.05)
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


#: How long a stopped task's processes get to exit on SIGTERM before SIGKILL.
TERMINATE_GRACE_SECONDS = 5.0


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#: Every task process currently running, so a force quit can take them along.
_LIVE: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()


def kill_running_tasks() -> None:
    """Kill every task process still running, at once. For a force quit only:
    tasks run in sessions of their own, so the terminal's Ctrl+C never reaches
    them, and os._exit leaves them running — after which the next start marks
    their runs failed and frees their slots and locks with the work still
    going."""
    with _LIVE_LOCK:
        processes = list(_LIVE)
    for process in processes:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, check=False, timeout=10)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass


def execute_command(spec: CommandSpec, cwd: Path, env: dict[str, str], stdout: Path,
                    stderr: Path, timeout: int | None,
                    should_stop: Callable[[], bool] | None = None,
                    mask: Callable[[], Masker] | None = None) -> ExecutionResult:
    """Run one task's subprocess to completion, a timeout, or a cancellation.

    `should_stop` is polled while the process runs. Without it, cancelling a run
    only marked it cancelled: this call blocked in wait() until the command
    finished on its own, so a long task kept running — for hours, with no
    timeout set — under a run the UI already showed as cancelled.

    With secrets to hide (`mask` returns a non-empty Masker), output goes
    through a pipe and is masked on its way to the log files; otherwise the
    process writes to them directly, as it always has.
    """
    if mask is not None and mask():
        return _execute_masked(spec, cwd, env, stdout, stderr, timeout, should_stop, mask)
    try:
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen(
                spec.command, shell=spec.shell, cwd=cwd, env=env, stdout=out, stderr=err,
                start_new_session=os.name != "nt",
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               if os.name == "nt" else 0),
            )
            with _LIVE_LOCK:
                _LIVE.add(process)
            try:
                return _supervise(process, timeout, should_stop)
            finally:
                with _LIVE_LOCK:
                    _LIVE.discard(process)
    except OSError as exc:
        return ExecutionResult(1, f"Could not start subprocess: {exc}")


#: After the process exits, how long its output gets to drain into the logs.
#: Longer only when something it started in the background still holds the
#: pipe; that output keeps arriving after the task is recorded as finished.
PUMP_DRAIN_SECONDS = 5.0


def _pump(stream, out, source: Callable[[], Masker]) -> None:
    """Copy one output stream to its log, masked. The pipe is drained to the
    end whatever happens to the file, or the process would block on a full
    pipe."""
    masker = StreamMasker(source)
    try:
        for chunk in iter(lambda: stream.read1(65536), b""):
            try:
                out.write(masker.feed(chunk))
                out.flush()
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        pass
    finally:
        try:
            out.write(masker.flush())
            out.close()
        except (OSError, ValueError):
            pass


def _execute_masked(spec: CommandSpec, cwd: Path, env: dict[str, str], stdout: Path,
                    stderr: Path, timeout: int | None,
                    should_stop: Callable[[], bool] | None,
                    mask: Callable[[], Masker]) -> ExecutionResult:
    try:
        out, err = stdout.open("wb"), stderr.open("wb")
    except OSError as exc:
        return ExecutionResult(1, f"Could not open the task's log files: {exc}")
    try:
        process = subprocess.Popen(
            spec.command, shell=spec.shell, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                           if os.name == "nt" else 0),
        )
    except OSError as exc:
        out.close(); err.close()
        return ExecutionResult(1, f"Could not start subprocess: {exc}")
    pumps = [threading.Thread(target=_pump, args=(stream, handle, mask), daemon=True,
                              name=f"runrail-task-log-{process.pid}")
             for stream, handle in ((process.stdout, out), (process.stderr, err))]
    for pump in pumps:
        pump.start()
    with _LIVE_LOCK:
        _LIVE.add(process)
    try:
        return _supervise(process, timeout, should_stop)
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(process)
        deadline = time.monotonic() + PUMP_DRAIN_SECONDS
        for pump in pumps:
            pump.join(max(0.0, deadline - time.monotonic()))


def _supervise(process: subprocess.Popen, timeout: int | None,
               should_stop: Callable[[], bool] | None) -> ExecutionResult:
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        slice_seconds = POLL_SECONDS
        if deadline is not None:
            slice_seconds = max(0.0, min(slice_seconds, deadline - time.monotonic()))
        try:
            return ExecutionResult(process.wait(timeout=slice_seconds))
        except subprocess.TimeoutExpired:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            _terminate(process)
            return ExecutionResult(124, f"Task timed out after {timeout} seconds")
        if should_stop is not None and _asked_to_stop(should_stop):
            _terminate(process)
            return ExecutionResult(130, "Cancelled while running", cancelled=True)


def _asked_to_stop(should_stop: Callable[[], bool]) -> bool:
    # A failed check (the database briefly locked, say) must not kill a healthy
    # task; the next poll asks again.
    try:
        return bool(should_stop())
    except Exception:
        return False


#: SQLite virtual-machine steps between checks for a timeout or a cancellation.
_SQL_CHECK_EVERY = 10_000


def execute_sql(task: Task, context: dict[str, Any], cwd: Path, stdout: Path,
                stderr: Path, timeout: int | None = None,
                should_stop: Callable[[], bool] | None = None,
                masker: Masker | None = None) -> ExecutionResult:
    """Run a task's SQL script against a SQLite database, in this process.

    There is no subprocess to kill, so timeouts and cancellation go through
    SQLite's progress handler, which aborts the statement in flight. Without
    it a SQL task ignored both: the form promised a timeout that "kills the
    task", and Cancel waited for the script to finish by itself. As with a
    killed shell script, statements that already completed stay committed.
    """
    params = context.get("parameters", {})
    db_path = params.get("sqlite_db_path") or os.environ.get("RUNRAIL_SQLITE_DB_PATH")
    if not db_path: return ExecutionResult(1, "SQL task requires sqlite_db_path parameter or RUNRAIL_SQLITE_DB_PATH")
    if not task.sql_path: return ExecutionResult(1, "SQL task requires sql_path")
    deadline = time.monotonic() + timeout if timeout else None
    stopped: list[ExecutionResult] = []
    next_poll = [0.0]

    def interrupt() -> int:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            stopped.append(ExecutionResult(124, f"Task timed out after {timeout} seconds"))
        elif should_stop is not None and now >= next_poll[0]:
            next_poll[0] = now + POLL_SECONDS  # the check reads the database
            if _asked_to_stop(should_stop):
                stopped.append(ExecutionResult(130, "Cancelled while running", cancelled=True))
        return 1 if stopped else 0

    connection = None
    try:
        script = (cwd / render(task.sql_path, context)).read_text()
        connection = sqlite3.connect(db_path)
        connection.set_progress_handler(interrupt, _SQL_CHECK_EVERY)
        with connection:
            connection.executescript(render(script, context))
        stdout.write_text("SQL script completed successfully\n")
        stderr.write_text("")
        return ExecutionResult(0)
    except Exception as exc:
        if stopped:
            stderr.write_text(f"{stopped[0].error}\n")
            return stopped[0]
        hide = masker.text if masker else (lambda text: text)
        stderr.write_text(hide(f"{type(exc).__name__}: {exc}\n"))
        return ExecutionResult(1, hide(str(exc)))
    finally:
        if connection is not None:
            connection.close()


def display_command(spec: CommandSpec) -> str:
    return spec.command if isinstance(spec.command, str) else shlex.join(spec.command)
