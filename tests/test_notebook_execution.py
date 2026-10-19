"""Notebooks, executed for real.

Everywhere else the suite stands in for the Jupyter stack — a stub renderer,
hand-written .ipynb files, environment builds with the runtime packages blanked
— so the path RunRail exists for had never run in CI on any platform: build a
managed environment, execute a notebook through nbexec and papermill in that
environment's kernel, watch it live, render the report, cancel it. Windows takes
a branch of its own there (TCP kernel channels; ZeroMQ has no ipc:// on Windows)
that nothing exercised at all.

Opt-in, because each test builds a virtualenv and installs papermill and
ipykernel from the network:

    RUNRAIL_NOTEBOOK_TESTS=1 pytest tests/test_notebook_execution.py
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from portable import process_alive
from test_reports import make_workflow

import runrail.environments as environments
from runrail.db import SessionLocal
from runrail.models import Environment
from runrail.reports import renderer_available
from runrail.worker.service import claim_runnable_run, execute_workflow_run

pytestmark = pytest.mark.skipif(not os.environ.get("RUNRAIL_NOTEBOOK_TESTS"),
                                reason="set RUNRAIL_NOTEBOOK_TESTS=1 to build real environments")

# Read at import, before conftest blanks it for every test that does not need it.
RUNTIME_PACKAGES = environments._RUNTIME_PACKAGES


def _notebook(path: Path, *cells: str, parameters: str | None = None) -> None:
    def code(source, tags=()):
        return {"cell_type": "code", "metadata": {"tags": list(tags)}, "source": source,
                "execution_count": None, "outputs": []}
    body = ([code(parameters, ["parameters"])] if parameters else []) + [code(c) for c in cells]
    path.write_text(json.dumps({
        "nbformat": 4, "nbformat_minor": 4,
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3",
                                    "language": "python"}},
        "cells": body,
    }))


def _stream_text(notebook: Path) -> str:
    cells = json.loads(notebook.read_text())["cells"]
    return "".join("".join(output.get("text", "")) for cell in cells
                   for output in cell.get("outputs", []) if output.get("output_type") == "stream")


@pytest.fixture()
def notebook_workflow(client, tmp_path, monkeypatch):
    """A project folder and a workflow whose default runtime is a freshly built
    managed environment, the way the Environments page makes one."""
    monkeypatch.setattr(environments, "_RUNTIME_PACKAGES", RUNTIME_PACKAGES)
    created = client.post("/api/environments", json={"name": "nb", "create_venv": True})
    assert created.status_code == 201, created.text
    with SessionLocal() as db:
        environment = environments.provision_managed(db, db.get(Environment, created.json()["id"]))
        assert environment.status.value == "ready", environment.build_log
        env_root = environments.managed_environment_path(environment)

    root = tmp_path / "project"
    root.mkdir()
    project = client.post("/api/projects", json={"name": "nb", "root_path": str(root)}).json()
    workflow = make_workflow(client, "notebooks", project_id=project["id"],
                             default_environment_id=created.json()["id"])
    return workflow, root, env_root


def _add_notebook_task(client, workflow, name, notebook, **extra):
    response = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": name, "task_type": "notebook", "notebook_path": notebook,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def test_a_notebook_runs_in_its_environment_and_renders(client, notebook_workflow):
    workflow, root, env_root = notebook_workflow
    _notebook(root / "report.ipynb",
              "import sys\nprint('region=' + region)\nprint('prefix=' + sys.prefix)",
              parameters="region = 'default'")
    _add_notebook_task(client, workflow, "report", "report.ipynb",
                       parameters_json={"region": "ca"})
    run_id = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()["id"]
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))

    run = client.get(f"/api/runs/{run_id}").json()
    task_run = run["task_runs"][0]
    stderr = client.get(f"/api/task-runs/{task_run['id']}/stderr").text
    assert run["status"] == "success", stderr

    artifacts = client.get(f"/api/runs/{run_id}/outputs").json()["reports"]
    assert len(artifacts) == 1
    output = _stream_text(_executed_notebook(run_id))
    assert "region=ca" in output, "the task's parameter was not injected"
    prefix = output.split("prefix=", 1)[1].splitlines()[0]
    assert Path(prefix).resolve() == env_root.resolve(), \
        "the kernel ran outside the task's environment"

    if renderer_available():
        report = client.get(f"/api/runs/{run_id}/report")
        assert report.status_code == 200 and "region=ca" in report.text


def _executed_notebook(run_id: int) -> Path:
    from runrail.config import get_settings
    found = sorted((get_settings().artifacts_dir.resolve() / str(run_id)).glob("*.ipynb"))
    assert found, "papermill wrote no output notebook"
    return found[0]


def test_cancelling_a_notebook_kills_its_kernel(client, notebook_workflow, tmp_path):
    """The kernel is papermill's child but, on POSIX, in a session of its own,
    so killing the task's process group does not reach it directly. It has to
    go anyway, or every cancelled notebook leaves a kernel behind."""
    workflow, root, _ = notebook_workflow
    pidfile = tmp_path / "kernel.pid"
    _notebook(root / "slow.ipynb",
              f"import os\nopen(r'{pidfile}', 'w').write(str(os.getpid()))",
              "import time\ntime.sleep(120)")
    _add_notebook_task(client, workflow, "slow", "slow.ipynb", retries=1)
    run_id = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()["id"]

    def work():
        with SessionLocal() as db:
            execute_workflow_run(db, claim_runnable_run(db))

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    deadline = time.monotonic() + 120  # kernel start-up is slow on a cold Windows runner
    while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.2)
    kernel = int(pidfile.read_text())
    assert process_alive(kernel)

    try:
        # The live view reads the notebook papermill rewrites after each cell.
        task_run_id = client.get(f"/api/runs/{run_id}").json()["task_runs"][0]["id"]
        progress_url = f"/api/task-runs/{task_run_id}/notebook-progress"
        deadline = time.monotonic() + 30
        progress = client.get(progress_url).json()
        while progress.get("completed", 0) < 1 and time.monotonic() < deadline:
            time.sleep(0.5)
            progress = client.get(progress_url).json()
        assert progress["available"] and progress["completed"] == 1 and progress["total"] == 2
        assert "time.sleep(120)" in progress["current"]["source"]

        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
        deadline = time.monotonic() + 15
        while process_alive(kernel) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not process_alive(kernel), "the notebook's kernel outlived the cancellation"

        worker.join(timeout=15)
        assert not worker.is_alive(), "the worker went on to a retry instead of stopping"
        rows = client.get(f"/api/runs/{run_id}").json()["task_runs"]
        assert [(row["attempt"], row["status"]) for row in rows] == [(1, "cancelled")]
    finally:
        if process_alive(kernel):
            os.kill(kernel, 9)


def test_a_secret_a_notebook_prints_is_masked_in_its_output_and_report(client, notebook_workflow):
    """The kernel reads the secret from its environment and prints it; papermill
    saves the output as printed. What RunRail keeps — the executed notebook,
    its report, its download — holds the mask instead."""
    from runrail.masking import MASK
    workflow, root, _ = notebook_workflow
    secret = "nb-Secret-9f3k2"
    assert client.post("/api/variables", json={"name": "NB_TOKEN", "value": secret,
                                               "secret": True}).status_code == 201
    _notebook(root / "leaky.ipynb",
              "import os\nprint('token=' + os.environ['NB_TOKEN'])\nos.environ['NB_TOKEN']")
    _add_notebook_task(client, workflow, "leaky", "leaky.ipynb")
    run_id = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()["id"]
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "success", client.get(f"/api/task-runs/{run['task_runs'][0]['id']}/stderr").text

    executed = _executed_notebook(run_id).read_text(encoding="utf-8")
    assert secret not in executed, "the executed notebook kept the secret"
    assert f"token={MASK}" in executed
    artifact = client.get(f"/api/runs/{run_id}/outputs").json()["reports"][0]
    download = client.get(f"/api/artifacts/{artifact['notebook_artifact_id']}/download").text
    assert secret not in download
    if renderer_available():
        report = client.get(f"/api/runs/{run_id}/report")
        assert report.status_code == 200 and secret not in report.text and MASK in report.text
