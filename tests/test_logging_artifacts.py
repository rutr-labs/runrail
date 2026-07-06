"""Tests for pipeline logging/artifact fixes: timestamped artifact names,
per-run log directories, log tailing, artifact filtering, and retention cleanup."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from runrail.models import Task, TaskType
from runrail.worker.runners import build_command


def make_workflow(client, name="wf"):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()


def make_shell_task(client, workflow_id, name, command="echo ok"):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    }).json()


def execute_queued_run(client):
    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run
    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None
        execute_workflow_run(db, run)
        return run.id


def test_notebook_artifact_name_uses_timestamp_and_task_run_id(tmp_path: Path):
    task = Task(name="report", task_type=TaskType.notebook, notebook_path="input.ipynb")
    context = {"artifacts_dir": str(tmp_path), "ds": "2026-07-02",
               "ts_nodash": "20260702T141005", "task_run_id": 42, "parameters": {}}
    spec = build_command(task, context, ["/managed/python"])
    assert spec.artifact is not None
    assert spec.artifact.name == "report_20260702T141005_42.ipynb"
    # A retry attempt has a new task-run id, so it cannot overwrite the first output.
    retry = build_command(task, {**context, "task_run_id": 43}, ["/managed/python"])
    assert retry.artifact.name != spec.artifact.name


def test_notebook_artifact_timestamp_cannot_escape_artifacts_directory(tmp_path: Path):
    task = Task(name="../../outside", task_type=TaskType.notebook, notebook_path="input.ipynb")
    spec = build_command(
        task,
        {"artifacts_dir": str(tmp_path), "ds": "2026-07-02", "ts_nodash": "../escape",
         "task_run_id": 1, "parameters": {}},
        ["/managed/python"],
    )
    assert spec.artifact.parent == tmp_path
    assert ".." not in spec.artifact.name


def test_logs_are_written_to_per_run_directory(client):
    workflow = make_workflow(client, "per-run-logs")
    make_shell_task(client, workflow["id"], "greet", command="printf hello")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)

    from runrail.config import get_settings
    from runrail.db import SessionLocal
    from runrail.models import WorkflowRun
    with SessionLocal() as db:
        stdout_path = Path(db.get(WorkflowRun, run_id).task_runs[0].stdout_log_path)
    log_dir = get_settings().logs_dir.resolve() / f"run_{run_id}"
    assert stdout_path.parent == log_dir
    assert stdout_path.read_text() == "hello"


def test_log_endpoint_supports_tailing(client):
    workflow = make_workflow(client, "tail-logs")
    make_shell_task(client, workflow["id"], "greet", command="printf 0123456789")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)

    task_run_id = client.get(f"/api/runs/{run_id}").json()["task_runs"][0]["id"]
    full = client.get(f"/api/task-runs/{task_run_id}/stdout")
    assert full.text == "0123456789"
    tail = client.get(f"/api/task-runs/{task_run_id}/stdout", params={"tail_bytes": 4})
    assert tail.text == "6789"


def test_empty_workflow_run_is_marked_failed_not_success(client):
    workflow = make_workflow(client, "empty-run")
    from runrail.api.crud import create_run
    from runrail.db import SessionLocal
    from runrail.models import TriggerType, Workflow
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run
    with SessionLocal() as db:
        run = create_run(db, db.get(Workflow, workflow["id"]), TriggerType.cli)
        claimed = claim_next_run(db)
        assert claimed is not None and claimed.id == run.id
        execute_workflow_run(db, claimed)
    assert client.get(f"/api/runs/{run.id}").json()["status"] == "failed"


def test_artifacts_endpoint_filters_by_run(client):
    from runrail.db import SessionLocal
    from runrail.models import Artifact

    workflow = make_workflow(client, "artifact-filter")
    make_shell_task(client, workflow["id"], "greet")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)

    with SessionLocal() as db:
        db.add(Artifact(workflow_run_id=run_id, name="a.ipynb", path="/tmp/a.ipynb"))
        db.add(Artifact(workflow_run_id=None, name="b.ipynb", path="/tmp/b.ipynb"))
        db.commit()

    scoped = client.get("/api/artifacts", params={"workflow_run_id": run_id}).json()
    assert [a["name"] for a in scoped] == ["a.ipynb"]
    assert len(client.get("/api/artifacts").json()) == 2


def test_scheduler_sync_keeps_internal_jobs_alive(client, monkeypatch):
    """Regression: sync() used to remove every job not named workflow-*, including
    the periodic 'sync' job itself — so workflows created after startup+30s were
    never scheduled until the process restarted."""
    monkeypatch.setenv("RUNRAIL_RETENTION_DAYS", "30")
    from runrail.config import get_settings
    get_settings.cache_clear()
    from runrail.scheduler.service import SchedulerService

    workflow = client.post("/api/workflows", json={
        "name": "cron-wf", "enabled": True, "max_concurrent_runs": 1,
        "schedule_cron": "*/2 * * * *",
    }).json()
    service = SchedulerService()
    service.start()
    try:
        service.sync()  # simulate the periodic sync job firing
        job_ids = {job.id for job in service.scheduler.get_jobs()}
        assert f"workflow-{workflow['id']}" in job_ids
        assert "sync" in job_ids
        assert "cleanup" in job_ids
    finally:
        service.shutdown()
        get_settings.cache_clear()


def test_cleanup_deletes_old_runs_and_their_files(client):
    workflow = make_workflow(client, "retention")
    make_shell_task(client, workflow["id"], "greet", command="printf old")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    old_run_id = execute_queued_run(client)
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    recent_run_id = execute_queued_run(client)

    from runrail.db import SessionLocal
    from runrail.maintenance import cleanup_runs
    from runrail.models import WorkflowRun

    with SessionLocal() as db:
        run = db.get(WorkflowRun, old_run_id)
        stdout_path = Path(run.task_runs[0].stdout_log_path)
        assert stdout_path.is_file()
        run.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
        db.commit()

    with SessionLocal() as db:
        dry = cleanup_runs(db, older_than_days=30, dry_run=True)
    assert dry.runs_deleted == 1
    assert client.get(f"/api/runs/{old_run_id}").status_code == 200  # dry run deletes nothing

    with SessionLocal() as db:
        stats = cleanup_runs(db, older_than_days=30)
    assert stats.runs_deleted == 1
    assert not stdout_path.exists()
    assert not stdout_path.parent.exists()
    assert client.get(f"/api/runs/{old_run_id}").status_code == 404
    assert client.get(f"/api/runs/{recent_run_id}").status_code == 200
