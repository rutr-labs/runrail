"""Which code a task ran: a project tracked whole, a loose script with what
it imports, secrets never read, and code kept exactly as long as a run
that is still kept points at it."""

import json
import shutil
import subprocess
import sys

import pytest

from runrail import codetrack
from runrail.db import SessionLocal
from runrail.maintenance import cleanup_runs
from runrail.models import CodeBlob, CodeManifest, TaskRun, WorkflowRun
from runrail.worker.queue import claim_next_run
from runrail.worker.service import execute_workflow_run

PY = f'"{sys.executable}"'
HAS_GIT = shutil.which("git") is not None


def make(client, name, command, cwd, **extra):
    workflow = client.post("/api/workflows", json={"name": name, "enabled": True, **extra}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": command, "cwd": str(cwd), "depends_on_json": []})
    return workflow


def run(client, workflow):
    run_id = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()["id"]
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    return client.get(f"/api/runs/{run_id}").json()


def code_change(client, after, before):
    tasks = client.get(f"/api/runs/{after['id']}/compare", params={"with": before["id"]}).json()["tasks"]
    return tasks[0].get("code")


def inputs(run_json):
    with SessionLocal() as db:
        return db.get(TaskRun, run_json["task_runs"][0]["id"]).inputs_json["code"]


def test_in_a_project_a_changed_helper_is_found_and_put_first(client, tmp_path):
    project = tmp_path / "orders"
    (project / "etl").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'orders'\n")
    (project / "etl" / "__init__.py").write_text("")
    (project / "etl" / "helpers.py").write_text("TABLE = 'orders'\n")
    (project / "load.py").write_text("from etl.helpers import TABLE\nprint(TABLE)\n")
    (project / "unrelated.py").write_text("print('someone else')\n")
    (project / "config.yaml").write_text("region: EU\n")
    workflow = make(client, "p", f'{PY} load.py', project)
    first = run(client, workflow)
    recorded = inputs(first)
    assert recorded["mode"] == "project" and recorded["entries"] == ["load.py"]
    assert recorded["used"] == ["etl/__init__.py", "etl/helpers.py", "load.py"]

    (project / "etl" / "helpers.py").write_text("TABLE = 'orders_v2'\n")
    (project / "unrelated.py").write_text("print('someone else, edited')\n")
    second = run(client, workflow)
    code = code_change(client, second, first)
    assert [(f["path"], f["used"]) for f in code["files"]] == [("etl/helpers.py", True), ("unrelated.py", False)]
    assert (code["used_changed"], code["other_changed"]) == (1, 1)
    assert "+TABLE = 'orders_v2'" in code["files"][0]["diff"]


def test_a_loose_script_brings_what_it_imports_and_opens_not_its_neighbours(client, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "util.py").write_text("def rows():\n    return 1\n")
    (shared / "settings.yaml").write_text("limit: 10\n")
    (shared / "report.py").write_text("import util\nprint(open('settings.yaml').read(), util.rows())\n")
    (shared / "someone_elses.py").write_text("print(1)\n")
    workflow = make(client, "loose", f'{PY} report.py', shared)
    first = run(client, workflow)
    assert first["status"] == "success"
    recorded = inputs(first)
    assert recorded["mode"] == "script"
    assert recorded["used"] == ["report.py", "settings.yaml", "util.py"]

    (shared / "someone_elses.py").write_text("print(2)\n")
    second = run(client, workflow)
    assert code_change(client, second, first) is None, "a neighbour it never reads is not its code"

    (shared / "settings.yaml").write_text("limit: 0\n")
    third = run(client, workflow)
    code = code_change(client, third, second)
    assert [f["path"] for f in code["files"]] == ["settings.yaml"]


def test_secret_files_are_never_read(client, tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "setup.py").write_text("")
    (project / ".env").write_text("DB_PASSWORD=hunter2\n")
    (project / "credentials.json").write_text('{"password": "hunter2"}')
    (project / "id_rsa.key").write_text("KEY")
    (project / "main.py").write_text("print(1)\n")
    first = run(client, make(client, "s", f'{PY} main.py', project))
    with SessionLocal() as db:
        files = db.get(CodeManifest, inputs(first)["manifest"]).files
        blobs = [b.content for b in db.query(CodeBlob)]
    assert set(files) == {"main.py", "setup.py"}
    assert not any(b"hunter2" in __import__("zlib").decompress(b) for b in blobs)


@pytest.mark.skipif(not HAS_GIT, reason="needs git")
def test_a_git_project_lists_files_as_git_does_and_names_the_commits(client, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / ".gitignore").write_text("scratch/\n")
    (repo / "scratch").mkdir()
    (repo / "scratch" / "notes.py").write_text("ignored\n")
    (repo / "job.py").write_text("print('v1')\n")
    git("add", ".")
    git("commit", "-qm", "one")
    workflow = make(client, "g", f'{PY} job.py', repo)
    first = run(client, workflow)
    recorded = inputs(first)
    assert recorded["git"]["dirty"] is False and len(recorded["git"]["commit"]) == 40
    with SessionLocal() as db:
        assert "scratch/notes.py" not in db.get(CodeManifest, recorded["manifest"]).files

    (repo / "job.py").write_text("print('v2')\n")
    second = run(client, workflow)
    assert inputs(second)["git"]["dirty"] is True, "an uncommitted edit is said to be one"
    code = code_change(client, second, first)
    assert code["git"]["before"]["commit"] == code["git"]["after"]["commit"]
    assert [f["path"] for f in code["files"]] == ["job.py"]


def test_a_notebook_is_its_code_not_its_saved_outputs(client, tmp_path):
    folder = tmp_path / "nb"
    folder.mkdir()
    notebook = folder / "report.ipynb"

    def write(source, output):
        notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
            {"cell_type": "code", "source": source, "metadata": {}, "execution_count": 3,
             "outputs": [{"output_type": "stream", "name": "stdout", "text": output}]}]}))

    write("print(1)", "1\n")
    workflow = make(client, "n", f'{PY} -c "pass" report.ipynb', folder)
    first = run(client, workflow)
    write("print(1)", "a different saved output\n")
    second = run(client, workflow)
    assert code_change(client, second, first) is None
    write("print(2)", "2\n")
    third = run(client, workflow)
    assert "+print(2)" in code_change(client, third, second)["files"][0]["diff"]


def test_two_tasks_storing_the_same_file_do_not_collide(client, tmp_path):
    (tmp_path / "x.py").write_text("print('same')\n")
    read = codetrack._stats.read(tmp_path / "x.py")
    with SessionLocal() as db:
        # Another task stored it a moment ago, between this one's look and its insert.
        db.add(CodeBlob(sha256=read.sha, size=read.size, content=b"x"))
        db.commit()
        codetrack._insert_missing(db, CodeBlob, [{"sha256": read.sha, "size": 1, "content": b"y"}])
        db.commit()
        assert db.get(CodeBlob, read.sha).content == b"x"


def test_unchanged_code_stores_nothing_new(client, tmp_path):
    (tmp_path / "x.py").write_text("print('same')\n")
    workflow = make(client, "u", f'{PY} x.py', tmp_path)
    first, second = run(client, workflow), run(client, workflow)
    assert inputs(first)["manifest"] == inputs(second)["manifest"]
    with SessionLocal() as db:
        assert db.query(CodeManifest).count() == 1 and db.query(CodeBlob).count() == 1


def test_code_lives_as_long_as_a_kept_run_points_at_it(client, tmp_path):
    from datetime import timedelta

    from runrail.models import now
    script = tmp_path / "job.py"
    script.write_text("print('old')\n")
    workflow = make(client, "r", f'{PY} job.py', tmp_path)
    old = run(client, workflow)
    script.write_text("raise SystemExit('new and broken')\n")
    broken = run(client, workflow)
    assert (old["status"], broken["status"]) == ("success", "failed")
    with SessionLocal() as db:
        # Both runs are past retention. The old one is the workflow's newest
        # success, which retention keeps, so its code must stay with it.
        db.query(WorkflowRun).update({WorkflowRun.finished_at: now() - timedelta(days=60)})
        db.commit()
        cleanup_runs(db, 30)
        assert db.get(WorkflowRun, broken["id"]) is None and db.get(WorkflowRun, old["id"]) is not None
        manifests = {m.sha256 for m in db.query(CodeManifest)}
        assert manifests == {inputs(old)["manifest"]}
        texts = [__import__("zlib").decompress(b.content).decode() for b in db.query(CodeBlob)]
        assert texts == ["print('old')\n"]


def test_a_task_with_no_code_of_its_own_records_none(client):
    workflow = client.post("/api/workflows", json={"name": "echo", "enabled": True}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": f'{PY} -c "print(1)"', "depends_on_json": []})
    done = run(client, workflow)
    with SessionLocal() as db:
        task_run = db.get(TaskRun, done["task_runs"][0]["id"])
        assert "code" not in task_run.inputs_json and task_run.code_manifest is None


# ─── Staying out of what is not code, and light where reading costs ─────


def _project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "main.py").write_text("import helper\nprint(helper.X)\n")
    (root / "helper.py").write_text("X = 1\n")
    (root / "other.py").write_text("print('not used')\n")
    return root


def _manifest_files(run_json):
    with SessionLocal() as db:
        return set(db.get(CodeManifest, inputs(run_json)["manifest"]).files)


def test_environments_are_skipped_by_what_makes_them_one_not_their_name(client, tmp_path):
    root = _project(tmp_path)
    for env, marker in (("myenv", "pyvenv.cfg"), ("tools", "conda-meta")):
        (root / env / "bin").mkdir(parents=True)
        (root / env / "bin" / "activate_this.py").write_text("x = 1\n")
        if marker == "conda-meta":
            (root / env / marker).mkdir()
        else:
            (root / env / marker).write_text("home = /usr\n")
    (root / "orders.egg-info").mkdir()
    (root / "orders.egg-info" / "setup.cfg").write_text("")
    (root / "big.json").write_text(json.dumps({"rows": list(range(30_000))}))
    first = run(client, make(client, "e", f'{PY} main.py', root))
    assert _manifest_files(first) == {"big.json", "helper.py", "main.py", "other.py", "pyproject.toml"}
    with SessionLocal() as db:
        assert db.get(CodeManifest, inputs(first)["manifest"]).files["big.json"][1] is False, \
            "a large JSON file is data: fingerprint only"


def test_on_a_network_drive_only_the_files_the_task_runs_are_read(client, tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(codetrack, "on_network_drive", lambda path: True)
    listed = []
    monkeypatch.setattr(codetrack, "_project_files", lambda scope: listed.append(scope) or [])
    workflow = make(client, "net", f'{PY} main.py', root)
    first = run(client, workflow)
    assert listed == [], "no folder is walked on a network drive"
    recorded = inputs(first)
    assert recorded["mode"] == "script" and "network drive" in recorded["note"]
    assert _manifest_files(first) == {"main.py", "helper.py"}
    (root / "other.py").write_text("print('edited')\n")
    assert code_change(client, run(client, workflow), first) is None


def test_a_slow_project_is_not_listed_again_for_a_while(client, tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(codetrack, "LIST_BUDGET_SECONDS", -1.0)
    workflow = make(client, "slow", f'{PY} main.py', root)
    assert "slow" in inputs(run(client, workflow))["note"]
    monkeypatch.setattr(codetrack, "LIST_BUDGET_SECONDS", 60.0)
    assert "slow" in inputs(run(client, workflow))["note"], "remembered for an hour"


def test_the_setting_turns_it_down_or_off(client, tmp_path):
    root = _project(tmp_path)
    workflow = make(client, "set", f'{PY} main.py', root)
    assert client.put("/api/settings", json={"code_tracking": "files"}).status_code == 200
    assert _manifest_files(run(client, workflow)) == {"main.py", "helper.py"}
    assert client.put("/api/settings", json={"code_tracking": "off"}).status_code == 200
    with SessionLocal() as db:
        done = run(client, workflow)
        assert "code" not in db.get(TaskRun, done["task_runs"][0]["id"]).inputs_json
    assert client.put("/api/settings", json={"code_tracking": "everything"}).status_code == 422


def test_a_cloud_placeholder_is_never_opened(tmp_path):
    class Stat:
        st_size, st_mtime_ns, st_file_attributes = 120, 1, 0x00400000

    class Placeholder:
        suffix = ".py"
        def stat(self):
            return Stat()
        def read_bytes(self):
            raise AssertionError("opening a placeholder downloads it")
        def __str__(self):
            return "C:/Users/me/OneDrive/job.py"

    read = codetrack._StatCache().read(Placeholder())
    assert read is not None and read.textual is False


def test_a_network_mount_is_recognised_by_its_filesystem(monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows asks the drive itself")
    from collections import namedtuple
    Part = namedtuple("Part", "device mountpoint fstype opts")
    import psutil
    monkeypatch.setattr(psutil, "disk_partitions", lambda all=True: [
        Part("/dev/disk1", "/", "apfs", ""), Part("//server/share", str(tmp_path / "share"), "smbfs", "")])
    monkeypatch.setattr(codetrack, "_mounts", (0.0, []))
    assert codetrack.on_network_drive(tmp_path / "share" / "etl") is True
    assert codetrack.on_network_drive(tmp_path / "local") is False
