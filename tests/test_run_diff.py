"""What changed since the last good run: recorded as tasks start, compared on
request, deterministic, and masked."""

import json
import sys
from datetime import timedelta

from runrail import rundiff
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.models import Artifact, ArtifactType, SourceSnapshot, TaskRun, now
from runrail.worker.queue import claim_next_run
from runrail.worker.service import execute_workflow_run

PY = f'"{sys.executable}"'


def workflow_with(client, name, command, **task):
    workflow = client.post("/api/workflows", json={"name": name, "enabled": True}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "load", "task_type": "shell", "command": command, "depends_on_json": [], **task})
    return workflow


def run(client, workflow, **parameters):
    run_id = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": parameters}).json()["id"]
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    return client.get(f"/api/runs/{run_id}").json()


def compare(client, run_id, **query):
    response = client.get(f"/api/runs/{run_id}/compare", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_failure_after_a_script_edit_shows_the_edit_and_the_new_error(client, tmp_path):
    script = tmp_path / "load.py"
    script.write_text("import datetime\n"
                      "print('started at', datetime.datetime.now().isoformat())\n"
                      "rows = 48210\n"
                      "print(f'loaded {rows} rows')\n")
    workflow = workflow_with(client, "etl", f'{PY} "{script}"', cwd=str(tmp_path))
    good = run(client, workflow)
    assert good["status"] == "success"
    script.write_text("import datetime\n"
                      "print('started at', datetime.datetime.now().isoformat())\n"
                      "rows = 0\n"
                      "print(f'loaded {rows} rows')\n"
                      "raise SystemExit('ValueError: no rows to load')\n")
    bad = run(client, workflow)
    assert bad["status"] == "failed"

    diff = compare(client, bad["id"])
    assert diff["baseline"]["id"] == good["id"]
    task = diff["tasks"][0]
    assert (task["before"]["status"], task["after"]["status"]) == ("success", "failed")
    source = task["sources"][0]
    assert source["change"] == "modified" and source["path"].endswith("load.py")
    assert "-rows = 48210" in source["diff"] and "+rows = 0" in source["diff"]
    logs = task["logs"]
    # A count that moved is one changed line; the timestamp line is not news.
    assert logs["changed"] == [{"stream": "stdout", "before": "loaded 48210 rows",
                                "after": "loaded 0 rows", "line": 2}]
    assert [line["text"] for line in logs["new"]] == ["ValueError: no rows to load"]
    assert logs["first_error"] == "ValueError: no rows to load"
    assert logs["gone"] == []
    assert "command" not in task, "the same command, with its own run ids, is not a change"
    assert "load.py (last good run)" in source["diff"][0], "the diff is labelled by file name"


def test_parameters_and_the_command_they_rendered_are_compared(client):
    workflow = workflow_with(client, "p", f'{PY} -c "print(\'{{{{ region }}}}\')"')
    good = run(client, workflow, region="EU")
    other = run(client, workflow, region="US", note="rerun")
    diff = compare(client, other["id"], **{"with": good["id"]})
    assert diff["parameters"] == [
        {"name": "note", "change": "added", "after": "rerun"},
        {"name": "region", "change": "changed", "before": "EU", "after": "US"}]
    command = diff["tasks"][0]["command"]
    assert "EU" in command["before"] and "US" in command["after"]


def test_a_run_with_nothing_good_before_it_says_so(client):
    workflow = workflow_with(client, "first", f'{PY} -c "raise SystemExit(1)"')
    bad = run(client, workflow)
    diff = compare(client, bad["id"])
    assert diff["baseline"] is None and "no successful run" in diff["note"]


def test_only_runs_of_the_same_workflow_compare(client):
    a = run(client, workflow_with(client, "a", f'{PY} -c "pass"'))
    b = run(client, workflow_with(client, "b", f'{PY} -c "pass"'))
    response = client.get(f"/api/runs/{a['id']}/compare", params={"with": b["id"]})
    assert response.status_code == 400


def test_a_secret_in_a_log_stays_masked_in_the_diff(client):
    from runrail.masking import MASK
    client.post("/api/variables", json={"name": "TOKEN", "value": "s3cr3t-token-x", "secret": True})
    workflow = workflow_with(client, "m", f'{PY} -c "import os, sys; print(sys.argv[1] + os.environ[\'TOKEN\'])" {{{{ word }}}}')
    good = run(client, workflow, word="ok")
    bad = run(client, workflow, word="changed")
    text = json.dumps(compare(client, bad["id"], **{"with": good["id"]}), ensure_ascii=False)
    assert "s3cr3t-token-x" not in text and MASK in text


def test_package_changes_read_as_upgrades_additions_and_removals():
    before = {"environment": {"name": "etl", "python": "3.11.4", "built_at": "a",
                              "packages": ["numpy==1.26.4", "pandas==2.1.0", "requests==2.31.0"]}}
    after = {"environment": {"name": "etl", "python": "3.11.9", "built_at": "b",
                             "packages": ["numpy==1.26.4", "pandas==2.2.1", "pyarrow==15.0.0"]}}
    change = rundiff._environment_changes(after, before)
    assert change["python"] == {"before": "3.11.4", "after": "3.11.9"}
    assert change["packages"] == [
        {"name": "pandas", "before": "pandas==2.1.0", "after": "pandas==2.2.1"},
        {"name": "pyarrow", "before": None, "after": "pyarrow==15.0.0"},
        {"name": "requests", "before": "requests==2.31.0", "after": None}]
    assert rundiff._environment_changes(before, before) is None


def test_a_task_in_an_environment_records_its_packages(client):
    environment = client.post("/api/environments", json={
        "name": "here", "env_type": "python", "executable": sys.executable}).json()
    workflow = client.post("/api/workflows", json={"name": "env", "enabled": True,
                                                   "default_environment_id": environment["id"]}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": f'{PY} -c "pass"', "depends_on_json": []})
    done = run(client, workflow)
    rundiff.wait_for_package_lists()
    with SessionLocal() as db:
        inputs = db.get(TaskRun, done["task_runs"][0]["id"]).inputs_json
    assert inputs["environment"]["name"] == "here"
    assert any(p.lower().startswith("sqlalchemy==") for p in inputs["environment"]["packages"])


def _notebook(cells):
    def code(source, output="", error=None, duration=1.0, tags=()):
        outputs = [{"output_type": "stream", "name": "stdout", "text": output}] if output else []
        if error:
            outputs.append({"output_type": "error", "ename": error[0], "evalue": error[1], "traceback": []})
        return {"cell_type": "code", "source": source, "outputs": outputs, "execution_count": 1,
                "metadata": {"tags": list(tags), "papermill": {"duration": duration,
                                                               "status": "failed" if error else "completed"}}}
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [code(**c) for c in cells]}


def test_notebook_cells_are_compared_by_their_code(client):
    workflow = client.post("/api/workflows", json={"name": "nb", "enabled": True}).json()
    task = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "report", "task_type": "shell", "command": f'{PY} -c "pass"', "depends_on_json": []}).json()
    good, bad = run(client, workflow), run(client, workflow)
    shapes = {
        good["id"]: [
            {"source": "region = 'EU'", "tags": ["injected-parameters"]},
            {"source": "import pandas as pd"},
            {"source": "df = load()\nprint(len(df))", "output": "48210\n", "duration": 3.0},
            {"source": "df.to_sql('t')", "duration": 2.0},
        ],
        bad["id"]: [
            {"source": "region = 'US'", "tags": ["injected-parameters"]},
            {"source": "import pandas as pd"},
            {"source": "df = load(region)\nprint(len(df))", "output": "0\n", "duration": 40.0},
            {"source": "df.to_sql('t')", "error": ("ValueError", "empty frame")},
        ],
    }
    with SessionLocal() as db:
        for run_id, cells in shapes.items():
            folder = get_settings().artifacts_dir.resolve() / str(run_id)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "report.ipynb"
            path.write_text(json.dumps(_notebook(cells)))
            task_run = db.query(TaskRun).filter(TaskRun.workflow_run_id == run_id).one()
            db.add(Artifact(task_run_id=task_run.id, workflow_run_id=run_id, name=path.name,
                            artifact_type=ArtifactType.notebook, path=str(path), size_bytes=1))
        from runrail.models import Task
        db.get(Task, task["id"]).task_type = "notebook"
        db.commit()
    notebook = compare(client, bad["id"], **{"with": good["id"]})["tasks"][0]["notebook"]
    assert notebook["failed_cell"] == 3
    cells = {c["after_index"]: c for c in notebook["cells"]}
    assert set(cells) == {2, 3}, "the injected parameters and an unchanged cell are not news"
    assert set(cells[2]["reasons"]) == {"code", "output", "slower"}
    assert "+df = load(region)" in cells[2]["source_diff"]
    assert (cells[2]["output_before"], cells[2]["output_after"]) == (["48210"], ["0"])
    assert cells[3]["reasons"] == ["failed", "output"] and cells[3]["error"] == "ValueError: empty frame"


def test_retention_drops_script_versions_nobody_used_since(client, tmp_path):
    with SessionLocal() as db:
        db.add(SourceSnapshot(sha256="a" * 64, path="old.py", size=1, content="x",
                              last_seen=now() - timedelta(days=40)))
        db.add(SourceSnapshot(sha256="b" * 64, path="new.py", size=1, content="y"))
        db.commit()
        from runrail.maintenance import cleanup_runs
        cleanup_runs(db, 30)
        assert [s.path for s in db.query(SourceSnapshot)] == ["new.py"]


def test_a_task_that_never_started_shows_only_that(client):
    workflow = client.post("/api/workflows", json={"name": "chain", "enabled": True}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "first", "task_type": "shell", "depends_on_json": [],
        "command": f'{PY} -c "import sys; sys.exit(int(sys.argv[1]))" {{{{ code }}}}'})
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "second", "task_type": "shell", "command": f'{PY} -c "print(1)"', "depends_on_json": ["first"]})
    good = run(client, workflow, code=0)
    bad = run(client, workflow, code=3)
    tasks = {t["name"]: t for t in compare(client, bad["id"], **{"with": good["id"]})["tasks"]}
    second = tasks["second"]
    assert second["after"]["status"] in ("skipped", "cancelled", "upstream_failed") or second["after"] is None
    assert not {"command", "logs", "sources", "inputs_unknown"} & set(second)
