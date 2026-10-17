"""Reports when a notebook task ran more than once.

Every report surface used to pick the FIRST notebook of a task: on a run whose
notebook failed and then succeeded on retry, the run page, the ?task= link,
/latest and the shared export all showed the traceback from the attempt that
failed, and the successful attempt could not be reached from anywhere — the
UI's tabs had the same name for both and selected by name, so the second tab
opened the first.
"""

import json

import pytest
from test_reports import (
    attach_notebook,
    execute_queued_run,
    make_shell_task,
    make_workflow,
)
from test_reports import renderer as _renderer  # noqa: F401  registers the fixture

from runrail.db import SessionLocal
from runrail.models import RunStatus, TaskRun, TaskRunStatus, TriggerType, WorkflowRun


def _retried_notebook_run(client):
    """A run whose notebook task failed once and then succeeded. The test
    renderer echoes each notebook's file stem, which is what tells the two
    documents apart in the assertions."""
    workflow = make_workflow(client, "retried-report")
    make_shell_task(client, workflow["id"], "build_report")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)
    first = client.get(f"/api/runs/{run_id}").json()["task_runs"][0]
    with SessionLocal() as db:
        db.get(TaskRun, first["id"]).status = TaskRunStatus.failed
        second = TaskRun(workflow_run_id=run_id, task_id=first["task_id"], attempt=2,
                         status=TaskRunStatus.success, resume_index=0)
        db.add(second); db.commit()
        second_id = second.id
    attach_notebook(run_id, first["id"], name="attempt1.ipynb", heading="ATTEMPT ONE FAILED")
    attach_notebook(run_id, second_id, name="attempt2.ipynb", heading="ATTEMPT TWO SUCCEEDED")
    return run_id, first["id"], second_id


@pytest.mark.usefixtures("_renderer")
def test_every_default_surface_shows_the_final_attempt(client):
    run_id, _, _ = _retried_notebook_run(client)
    for url, params in ((f"/api/runs/{run_id}/report", None),
                        (f"/api/runs/{run_id}/report", {"task": "build_report"})):
        body = client.get(url, params=params).text
        assert "<p>attempt2</p>" in body, f"{url} {params} showed the failed attempt"
        assert "<p>attempt1</p>" not in body


@pytest.mark.usefixtures("_renderer")
def test_each_attempt_is_addressable_and_described(client):
    run_id, first_id, second_id = _retried_notebook_run(client)
    entries = client.get(f"/api/runs/{run_id}/outputs").json()["reports"]
    by_id = {entry["task_run_id"]: entry for entry in entries}

    assert by_id[first_id]["attempt"] == 1 and by_id[first_id]["status"] == "failed"
    assert by_id[second_id]["attempt"] == 2 and by_id[second_id]["final"] is True
    assert by_id[first_id]["final"] is False

    # The two entries must point at different documents.
    assert "<p>attempt1</p>" in client.get(by_id[first_id]["report_url"]).text
    assert "<p>attempt2</p>" in client.get(by_id[second_id]["report_url"]).text
    # The final attempt keeps the readable, shareable form.
    assert by_id[second_id]["report_url"].endswith("?task=build_report")


def test_live_progress_reads_a_notebook_mid_execution(client, tmp_path):
    """Papermill rewrites the output after every cell, with per-cell status in
    its metadata. The live view reads that file without rendering it."""
    from runrail.config import get_settings

    workflow = make_workflow(client, "live-notebook")
    task = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "crunch", "task_type": "notebook", "notebook_path": "x.ipynb"}).json()
    # Rows written directly: a notebook task with no environment is correctly
    # refused by /run, and only the progress reader is under test here.
    with SessionLocal() as db:
        parent = WorkflowRun(workflow_id=workflow["id"], status=RunStatus.running,
                             trigger_type=TriggerType.manual)
        db.add(parent); db.flush()
        row = TaskRun(workflow_run_id=parent.id, task_id=task["id"], attempt=1,
                      status=TaskRunStatus.running, resume_index=0)
        db.add(row); db.commit()
        run = {"id": parent.id}
        task_run_id = row.id

    url = f"/api/task-runs/{task_run_id}/notebook-progress"
    assert client.get(url).json()["available"] is False, "nothing is written yet"

    def cell(state, source, output=None):
        return {"cell_type": "code", "metadata": {"papermill": {"status": state}},
                "source": source, "execution_count": 1 if state == "completed" else None,
                "outputs": [{"output_type": "stream", "name": "stdout", "text": output}] if output else []}

    directory = get_settings().artifacts_dir.resolve() / str(run["id"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"crunch_20260101T000000_{task_run_id}.ipynb").write_text(json.dumps({
        "nbformat": 4, "nbformat_minor": 5, "metadata": {},
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": "# not counted"},
            cell("completed", "load()", "loaded 3 rows\n"),
            cell("running", "transform()"),
            cell("pending", "publish()"),
        ],
    }))
    progress = client.get(url).json()
    assert progress["available"] is True
    assert (progress["completed"], progress["total"]) == (1, 3), "markdown cells do not count"
    assert progress["current"]["index"] == 2 and progress["current"]["source"] == "transform()"
    assert progress["recent_output"] == ["loaded 3 rows"]


def test_live_progress_refuses_a_task_that_is_not_a_notebook(client):
    workflow = make_workflow(client, "shell-only")
    make_shell_task(client, workflow["id"], "t")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)
    task_run_id = client.get(f"/api/runs/{run_id}").json()["task_runs"][0]["id"]
    assert client.get(f"/api/task-runs/{task_run_id}/notebook-progress").status_code == 409
