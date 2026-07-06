import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        command = [*(python_command or [sys.executable]), "-m", "papermill",
                   render(task.notebook_path, context), str(output), "--kernel", "python3"]
        for key, value in context.get("parameters", {}).items(): command += ["-p", key, str(value)]
        return CommandSpec(command, False, output)
    raise ValueError(f"Task type {task.task_type.value} is handled separately")


def execute_command(spec: CommandSpec, cwd: Path, env: dict[str, str], stdout: Path,
                    stderr: Path, timeout: int | None) -> ExecutionResult:
    try:
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen(
                spec.command, shell=spec.shell, cwd=cwd, env=env, stdout=out, stderr=err,
                start_new_session=os.name != "nt",
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               if os.name == "nt" else 0),
            )
            try:
                return ExecutionResult(process.wait(timeout=timeout))
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, check=False,
                    )
                else:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                process.wait()
                return ExecutionResult(124, f"Task timed out after {timeout} seconds")
    except OSError as exc:
        return ExecutionResult(1, f"Could not start subprocess: {exc}")


def execute_sql(task: Task, context: dict[str, Any], cwd: Path, stdout: Path,
                stderr: Path) -> ExecutionResult:
    params = context.get("parameters", {})
    db_path = params.get("sqlite_db_path") or os.environ.get("RUNRAIL_SQLITE_DB_PATH")
    if not db_path: return ExecutionResult(1, "SQL task requires sqlite_db_path parameter or RUNRAIL_SQLITE_DB_PATH")
    if not task.sql_path: return ExecutionResult(1, "SQL task requires sql_path")
    try:
        script = (cwd / render(task.sql_path, context)).read_text()
        with sqlite3.connect(db_path) as connection:
            connection.executescript(render(script, context))
        stdout.write_text("SQL script completed successfully\n")
        stderr.write_text("")
        return ExecutionResult(0)
    except Exception as exc:
        stderr.write_text(f"{type(exc).__name__}: {exc}\n")
        return ExecutionResult(1, str(exc))


def display_command(spec: CommandSpec) -> str:
    return spec.command if isinstance(spec.command, str) else shlex.join(spec.command)
