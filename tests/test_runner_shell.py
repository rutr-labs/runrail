import os
import sys
from pathlib import Path

import pytest
from portable import WINDOWS, emit, process_alive, record_pid_and_sleep

from runrail.models import Task, TaskType
from runrail.worker.runners import CommandSpec, build_command, execute_command, python_module_name


def test_shell_success_and_failure(tmp_path: Path):
    out, err = tmp_path / "out", tmp_path / "err"
    result = execute_command(CommandSpec(emit("hello"), True), tmp_path, os.environ.copy(), out, err, 5)
    assert result.exit_code == 0 and out.read_text() == "hello"
    result = execute_command(CommandSpec("exit 7", True), tmp_path, os.environ.copy(), out, err, 5)
    assert result.exit_code == 7


def test_timeout_returns_124(tmp_path: Path):
    out, err = tmp_path / "out", tmp_path / "err"
    result = execute_command(
        CommandSpec([sys.executable, "-c", "import time; time.sleep(30)"], False),
        tmp_path, os.environ.copy(), out, err, 1,
    )
    assert result.exit_code == 124


def test_timeout_kills_the_process_the_command_started(tmp_path: Path):
    """The shell is only the parent. A timeout has to stop the process doing the
    work — on Windows a child of cmd.exe, which only taskkill /T reaches."""
    out, err, pidfile = tmp_path / "out", tmp_path / "err", tmp_path / "pid"
    result = execute_command(CommandSpec(record_pid_and_sleep(pidfile, 60), True), tmp_path,
                             os.environ.copy(), out, err, 3)
    assert result.exit_code == 124
    pid = int(pidfile.read_text())
    assert not process_alive(pid), "the task's process outlived its timeout"


@pytest.mark.skipif(not WINDOWS, reason="cmd.exe is the Windows shell")
def test_shell_tasks_run_in_cmd_on_windows(tmp_path: Path):
    """What a Windows user writes in a shell task is a cmd.exe command line."""
    out, err = tmp_path / "out", tmp_path / "err"
    result = execute_command(CommandSpec("echo %OS%&& exit 3", True), tmp_path,
                             os.environ.copy(), out, err, 10)
    assert out.read_text().strip() == "Windows_NT"
    assert result.exit_code == 3


def test_python_module_name_for_package_relative_imports(tmp_path: Path):
    package = tmp_path / "jobs"
    package.mkdir()
    (package / "__init__.py").touch()
    script = package / "daily.py"
    script.touch()
    assert python_module_name(script, tmp_path) == "jobs.daily"
    assert python_module_name(tmp_path / "standalone.py", tmp_path) is None


def test_notebook_command_forces_selected_kernel(tmp_path: Path):
    task = Task(name="notebook", task_type=TaskType.notebook, notebook_path="input.ipynb")
    spec = build_command(
        task,
        {"artifacts_dir": str(tmp_path), "ds": "2026-07-01", "parameters": {}},
        ["/managed/python"],
    )
    assert spec.command[0] == "/managed/python"
    # The nbexec shim (not `-m papermill`) launches the kernel on IPC transport.
    assert spec.command[1].endswith("nbexec.py")
    assert spec.command[-2:] == ["--kernel", "python3"]


def test_notebook_artifact_name_cannot_escape_artifacts_directory(tmp_path: Path):
    task = Task(name="../../outside", task_type=TaskType.notebook, notebook_path="input.ipynb")
    spec = build_command(
        task,
        {"artifacts_dir": str(tmp_path), "ds": "../date", "parameters": {}},
        ["/managed/python"],
    )
    assert spec.artifact is not None
    assert spec.artifact.parent == tmp_path
    assert ".." not in spec.artifact.name
