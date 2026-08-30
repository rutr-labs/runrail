import os
import sys
from pathlib import Path

from runrail.models import Task, TaskType
from runrail.worker.runners import CommandSpec, build_command, execute_command, python_module_name


def test_shell_success_and_failure(tmp_path: Path):
    out, err = tmp_path / "out", tmp_path / "err"
    result = execute_command(CommandSpec("printf hello", True), tmp_path, os.environ.copy(), out, err, 5)
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
