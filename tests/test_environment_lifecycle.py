"""Managed environments over their whole life: built, used, rebuilt, deleted.

Each test here is a defect that left a working environment unusable:

- A rebuild requested while a run was using the environment flipped it to
  `creating` straight away, and every remaining task of that run failed with
  "not ready" — while the build itself correctly waited for the run to finish
  and the old venv sat there intact.
- A queued run waiting on a build was claimed, requeued and claimed again on
  every poll, as the oldest in the queue, so unrelated workflows behind it
  never started.
- Builds were made in a staging directory and renamed into place, and every
  console script a venv installs hard-codes the interpreter path it was
  installed with: after the rename, `pytest`, `dbt`, `papermill` and anything
  else a shell task called failed with "bad interpreter".
- Deleting an environment never checked whether a running task was using it.
- A build interrupted by a restart stayed `building`, locked against edits,
  for thirty minutes.
"""

import base64
import hashlib
import zipfile
from datetime import timedelta
from pathlib import Path

from test_interactions import make_shell_task, make_workflow, start_run

import runrail.environments as environments
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.models import Environment, EnvironmentStatus, now
from runrail.worker.service import claim_runnable_run, execute_workflow_run


def _build(environment_id: int) -> Environment:
    """What the worker does with a claimed build, synchronously."""
    with SessionLocal() as db:
        claimed = environments.claim_next_environment(db)
        assert claimed is not None and claimed.id == environment_id
        return environments.provision_managed(db, claimed)


def _managed(client, name="env", packages=()) -> dict:
    created = client.post("/api/environments", json={
        "name": name, "create_venv": True, "packages": list(packages)})
    assert created.status_code == 201, created.text
    built = _build(created.json()["id"])
    assert built.status == EnvironmentStatus.ready, built.build_log
    return client.get(f"/api/environments/{built.id}").json()


def _wheel_with_a_cli(directory: Path) -> Path:
    """A wheel declaring one console script, built by hand so the test needs no
    network and no build backend."""
    files = {
        "hello_cli/__init__.py": "def main():\n    print('hello from the cli')\n",
        "hello_cli-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: hello-cli\nVersion: 1.0\n",
        "hello_cli-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: hand\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "hello_cli-1.0.dist-info/entry_points.txt": "[console_scripts]\nhello-cli = hello_cli:main\n",
    }
    record = []
    for name, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        record.append(f"{name},sha256={digest.decode()},{len(text.encode())}")
    record.append("hello_cli-1.0.dist-info/RECORD,,")
    path = directory / "hello_cli-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        for name, text in files.items():
            wheel.writestr(name, text)
        wheel.writestr("hello_cli-1.0.dist-info/RECORD", "\n".join(record) + "\n")
    return path


def _task_rows(client, run_id):
    return [(row["task_name"], row["status"]) for row in
            client.get(f"/api/runs/{run_id}").json()["task_runs"]]


def test_a_rebuild_requested_mid_run_waits_for_the_run_instead_of_failing_it(client):
    environment = _managed(client)
    workflow = make_workflow(client, "uses-env", default_environment_id=environment["id"])
    make_shell_task(client, workflow["id"], "first", "exit 0")
    make_shell_task(client, workflow["id"], "second", "exit 0", depends_on=["first"])
    run_id = start_run(client, workflow["id"])

    with SessionLocal() as db:
        run = claim_runnable_run(db)
        assert run is not None and run.id == run_id
        # The rebuild lands after the run has started.
        assert client.post(f"/api/environments/{environment['id']}/rebuild",
                           json={}).status_code == 200
        # ...and is deferred while the run is using the environment.
        assert environments.claim_next_environment(db) is None
        execute_workflow_run(db, run)

    assert client.get(f"/api/runs/{run_id}").json()["status"] == "success", _task_rows(client, run_id)
    # Once the run is done the deferred build goes ahead.
    assert _build(environment["id"]).status == EnvironmentStatus.ready


def test_a_run_waiting_on_a_build_does_not_hold_up_other_workflows(client):
    building = _managed(client, "slow-build")
    blocked = make_workflow(client, "needs-build", default_environment_id=building["id"])
    make_shell_task(client, blocked["id"], "t", "exit 0")
    free = make_workflow(client, "independent")
    make_shell_task(client, free["id"], "t", "exit 0")
    blocked_run = start_run(client, blocked["id"])  # oldest in the queue
    free_run = start_run(client, free["id"])
    # A rebuild queued after it: that run now has to wait for the build.
    assert client.post(f"/api/environments/{building['id']}/rebuild", json={}).status_code == 200

    with SessionLocal() as db:
        claimed = claim_runnable_run(db)
        assert claimed is not None and claimed.id == free_run, \
            "the run behind a pending build blocked an unrelated workflow"
    assert client.get(f"/api/runs/{blocked_run}").json()["status"] == "queued"


def test_console_scripts_in_a_managed_environment_run(client, tmp_path):
    environment = _managed(client, packages=[str(_wheel_with_a_cli(tmp_path))])
    workflow = make_workflow(client, "cli", default_environment_id=environment["id"])
    make_shell_task(client, workflow["id"], "call-cli", "hello-cli")
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))

    task_run = client.get(f"/api/runs/{run_id}").json()["task_runs"][0]
    stderr = client.get(f"/api/task-runs/{task_run['id']}/stderr").text
    assert task_run["status"] == "success", stderr
    assert "hello from the cli" in client.get(f"/api/task-runs/{task_run['id']}/stdout").text

    # The same after a rebuild, which is where a rename used to break them.
    client.post(f"/api/environments/{environment['id']}/rebuild", json={})
    assert _build(environment["id"]).status == EnvironmentStatus.ready
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))
    assert _task_rows(client, run_id) == [("call-cli", "success")]


def test_a_rebuild_leaves_one_directory_and_a_failed_one_keeps_the_old(client):
    environment = _managed(client)
    root = get_settings().environments_dir.resolve()
    first = Path(environment["executable"])

    client.post(f"/api/environments/{environment['id']}/rebuild", json={})
    rebuilt = _build(environment["id"])
    assert rebuilt.status == EnvironmentStatus.ready
    assert [p.name for p in root.iterdir()] == [Path(rebuilt.executable).parents[1].name], \
        "a rebuild left the previous environment, or a staging copy, on disk"
    assert not first.exists()

    client.post(f"/api/environments/{environment['id']}/rebuild",
                json={"packages": ["definitely-not-a-real-package-runrail==0.0.0"]})
    failed = _build(environment["id"])
    assert failed.status == EnvironmentStatus.degraded
    assert failed.executable == rebuilt.executable and Path(failed.executable).is_file(), \
        "a failed rebuild took the working environment with it"
    assert [p.name for p in root.iterdir()] == [Path(rebuilt.executable).parents[1].name]


def test_deleting_an_environment_a_running_task_uses_is_refused(client):
    environment = _managed(client)
    workflow = make_workflow(client, "busy", default_environment_id=environment["id"])
    make_shell_task(client, workflow["id"], "t", "exit 0")
    start_run(client, workflow["id"])
    with SessionLocal() as db:
        run = claim_runnable_run(db)
        response = client.delete(f"/api/environments/{environment['id']}")
        assert response.status_code == 409, response.text
        assert Path(environment["executable"]).is_file()
        execute_workflow_run(db, run)
    # Free again once nothing is running in it, and the directory goes with it.
    assert client.delete(f"/api/environments/{environment['id']}").status_code == 204
    assert list(get_settings().environments_dir.resolve().iterdir()) == []


def test_a_build_abandoned_by_a_restart_is_picked_up_again_within_minutes(client):
    environment = _managed(client)
    with SessionLocal() as db:
        row = db.get(Environment, environment["id"])
        # What a process killed mid-pip leaves behind: a claim nobody holds,
        # and a half-built directory.
        row.status = EnvironmentStatus.building
        row.build_started_at = now() - environments.BUILD_STALE_AFTER - timedelta(seconds=5)
        db.commit()
    leftover = get_settings().environments_dir.resolve() / f"env-{environment['id']}-deadbeef"
    leftover.mkdir()

    rebuilt = _build(environment["id"])
    assert rebuilt.status == EnvironmentStatus.ready
    assert not leftover.exists(), "the interrupted build's directory was never cleaned up"
    assert environments.BUILD_STALE_AFTER <= timedelta(minutes=5)


def test_a_long_build_keeps_its_claim(client, monkeypatch):
    """The stale-claim reset must not hand a build that is still running to a
    second thread: the build refreshes its claim while it works."""
    monkeypatch.setattr(environments, "BUILD_HEARTBEAT_SECONDS", 0.05)
    created = client.post("/api/environments", json={"name": "slow", "create_venv": True}).json()
    seen = []
    real_run = environments._run

    def slow_run(command, timeout):
        if "venv" in command:
            import time
            with SessionLocal() as db:
                before = db.get(Environment, created["id"]).build_started_at
            time.sleep(0.4)
            with SessionLocal() as db:
                seen.append((before, db.get(Environment, created["id"]).build_started_at))
        return real_run(command, timeout)

    monkeypatch.setattr(environments, "_run", slow_run)
    assert _build(created["id"]).status == EnvironmentStatus.ready
    (before, after), = seen
    assert after > before, "the claim was not refreshed during the build"

