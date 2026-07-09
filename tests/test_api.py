from pathlib import Path


def build_pending_environment():
    from runrail.db import SessionLocal
    from runrail.environments import claim_next_environment, provision_managed

    with SessionLocal() as db:
        environment = claim_next_environment(db)
        assert environment is not None
        provision_managed(db, environment)


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_filesystem_browser_is_confined_to_configured_root(client, tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "daily.py").write_text("print('ok')")
    listing = client.get("/api/filesystem", params={"path": str(tmp_path / "jobs")})
    assert listing.status_code == 200
    assert listing.json()["entries"][0]["name"] == "daily.py"
    assert client.get("/api/filesystem", params={"path": str(tmp_path.parent)}).status_code == 403


def test_managed_environment_is_created_and_invalid_python_is_rejected(client, tmp_path):
    created = client.post("/api/environments", json={
        "name": "default-jobs", "env_type": "python", "create_venv": True,
        "packages": [],
    })
    assert created.status_code == 201
    assert created.json()["status"] == "creating"
    build_pending_environment()
    payload = client.get(f"/api/environments/{created.json()['id']}").json()
    assert payload["managed"] is True
    assert payload["status"] == "ready"
    assert payload["python_version"]
    assert payload["packages_json"] == []
    executable = Path(payload["executable"])
    assert executable.is_file()
    assert tmp_path / ".runrail" / "environments" in executable.parents

    invalid = client.post("/api/environments", json={
        "name": "broken", "env_type": "python", "executable": str(tmp_path / "missing"),
    })
    assert invalid.status_code == 400


def test_managed_environment_rebuild_preserves_last_working_runtime(client):
    created = client.post("/api/environments", json={
        "name": "rebuildable", "env_type": "python", "create_venv": True,
    }).json()
    build_pending_environment()
    created = client.get(f"/api/environments/{created['id']}").json()
    original_executable = created["executable"]

    rejected = client.post(f"/api/environments/{created['id']}/rebuild", json={
        "packages": ["--invalid-pip-option"],
    })
    assert rejected.status_code == 400  # malformed specs fail fast at the API

    bogus = "runrail-package-that-does-not-exist==99.99"
    failed = client.post(f"/api/environments/{created['id']}/rebuild", json={
        "packages": [bogus],
    })
    assert failed.status_code == 200
    assert failed.json()["status"] == "creating"
    build_pending_environment()
    failed_payload = client.get(f"/api/environments/{created['id']}").json()
    assert failed_payload["status"] == "degraded"
    assert failed_payload["executable"] == original_executable
    assert "pip exited" in failed_payload["last_error"]
    assert Path(original_executable).is_file()
    assert failed_payload["active_packages_json"] == []
    assert failed_payload["packages_json"] == [bogus]

    rebuilt = client.post(f"/api/environments/{created['id']}/rebuild", json={
        "packages": [],
    })
    assert rebuilt.status_code == 200
    assert rebuilt.json()["status"] == "creating"
    build_pending_environment()
    rebuilt_payload = client.get(f"/api/environments/{created['id']}").json()
    assert rebuilt_payload["status"] == "ready"
    assert rebuilt_payload["last_error"] is None


def test_failed_environment_cannot_be_attached(client):
    failed = client.post("/api/environments", json={
        "name": "failed-build", "env_type": "python", "create_venv": True,
        "packages": ["runrail-package-that-does-not-exist==99.99"],
    })
    assert failed.status_code == 201
    assert failed.json()["status"] == "creating"
    build_pending_environment()
    failed_payload = client.get(f"/api/environments/{failed.json()['id']}").json()
    assert failed_payload["status"] == "failed"

    workflow = client.post("/api/workflows", json={
        "name": "unsafe-runtime", "enabled": True, "max_concurrent_runs": 1,
        "default_environment_id": failed_payload["id"],
    })
    assert workflow.status_code == 409
    assert "is not ready" in workflow.json()["detail"]


def test_managed_environment_update_tolerates_legacy_null_type(client):
    created = client.post("/api/environments", json={
        "name": "editable", "env_type": "python", "create_venv": True,
    }).json()
    build_pending_environment()
    updated = client.put(f"/api/environments/{created['id']}", json={
        "name": "edited", "env_type": "null", "description": "updated",
        "executable": "/tmp/must-not-replace-managed-python",
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "edited"
    assert updated.json()["env_type"] == "python"
    assert updated.json()["executable"] != "/tmp/must-not-replace-managed-python"


def test_create_workflow_task_run_and_backfill(client):
    workflow = client.post("/api/workflows", json={"name": "daily", "enabled": True,
        "max_concurrent_runs": 1}).json()
    assert workflow["name"] == "daily"
    task = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "hello", "task_type": "shell", "command": "echo hello",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 60}).json()
    assert task["workflow_id"] == workflow["id"]
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    assert run.status_code == 201 and run.json()["status"] == "queued"
    backfill = client.post(f"/api/workflows/{workflow['id']}/backfill", json={
        "from": "2026-06-01", "to": "2026-06-03", "parameters": {}})
    assert backfill.status_code == 201 and len(backfill.json()) == 3


def test_python_workflow_without_environment_is_rejected_before_queueing(client):
    workflow = client.post("/api/workflows", json={
        "name": "missing-runtime", "enabled": True, "max_concurrent_runs": 1,
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "python", "task_type": "python", "script_path": "job.py",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    assert run.status_code == 409
    assert "requires an execution environment" in run.json()["detail"]


def test_worker_executes_a_queued_workflow(client):
    workflow = client.post("/api/workflows", json={
        "name": "worker-test", "enabled": True, "max_concurrent_runs": 1,
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "hello", "task_type": "shell", "command": "printf hello",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    created = client.post(
        f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}
    ).json()

    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run

    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None and run.id == created["id"]
        execute_workflow_run(db, run)
        db.refresh(run)
        assert run.status.value == "success"
        assert run.task_runs[0].status.value == "success"


def test_managed_environment_runs_python_with_package_imports(client, tmp_path):
    package = tmp_path / "project" / "jobs"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "shared.py").write_text("MESSAGE = 'environment works'\n")
    source_root = tmp_path / "project" / "src"
    source_root.mkdir()
    (source_root / "project_setting.py").write_text("SUFFIX = ' with src layout'\n")
    script = package / "daily.py"
    script.write_text(
        "from .shared import MESSAGE\n"
        "from project_setting import SUFFIX\n"
        "print(MESSAGE + SUFFIX)\n"
    )
    (tmp_path / "project" / "pyproject.toml").write_text("[project]\nname='worker-project'\nversion='0'\n")

    environment = client.post("/api/environments", json={
        "name": "workflow-python", "env_type": "python", "create_venv": True,
    }).json()
    build_pending_environment()
    environment = client.get(f"/api/environments/{environment['id']}").json()
    project = client.post("/api/projects", json={
        "name": "python-project", "root_path": str(tmp_path / "project"),
        "default_environment_id": environment["id"],
    }).json()
    workflow = client.post("/api/workflows", json={
        "name": "python-workflow", "enabled": True, "max_concurrent_runs": 1,
        "project_id": project["id"],
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "daily", "task_type": "python", "script_path": str(script),
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    created = client.post(
        f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}
    ).json()

    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run

    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None and run.id == created["id"]
        execute_workflow_run(db, run)
        db.refresh(run)
        assert run.status.value == "success"
        assert Path(run.task_runs[0].stdout_log_path).read_text() == "environment works with src layout\n"


def test_daily_stats_aggregates_runs_by_outcome(client):
    workflow = client.post("/api/workflows", json={
        "name": "stats-wf", "enabled": True, "max_concurrent_runs": 1,
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "ok", "task_type": "shell", "command": "printf hi",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    for _ in range(2):
        client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
        build = __import__("runrail.db", fromlist=["SessionLocal"])
        from runrail.worker.queue import claim_next_run
        from runrail.worker.service import execute_workflow_run
        with build.SessionLocal() as db:
            execute_workflow_run(db, claim_next_run(db))

    stats = client.get("/api/stats/daily", params={"days": 7}).json()
    assert len(stats) == 1
    assert stats[0]["success"] == 2 and stats[0]["failed"] == 0
    scoped = client.get("/api/stats/daily", params={"workflow_id": workflow["id"] + 1}).json()
    assert scoped == []
