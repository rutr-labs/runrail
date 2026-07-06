"""Regression tests for production-readiness fixes: SPA path confinement,
task validation, dependency hygiene, and run cancellation."""


def make_workflow(client, name="wf"):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()


def make_shell_task(client, workflow_id, name, depends_on=None, command="echo ok"):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": depends_on or [], "retries": 0, "retry_delay_seconds": 0,
    })


def test_spa_catch_all_does_not_serve_files_outside_static_root(client):
    response = client.get("/%2e%2e/%2e%2e/config.py")
    assert "lru_cache" not in response.text  # runrail/config.py must not leak
    response = client.get("/%2e%2e/%2e%2e/%2e%2e/%2e%2e/pyproject.toml")
    assert "hatchling" not in response.text


def test_unknown_api_path_returns_404_not_spa_index(client):
    assert client.get("/api/does-not-exist").status_code == 404


def test_tasks_missing_their_source_are_rejected(client):
    workflow = make_workflow(client, "validation")
    no_command = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "bad-shell", "task_type": "shell",
    })
    assert no_command.status_code == 422
    no_sql = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "bad-sql", "task_type": "sql",
    })
    assert no_sql.status_code == 422
    no_notebook = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "bad-nb", "task_type": "notebook",
    })
    assert no_notebook.status_code == 422


def test_dependencies_are_validated_at_write_time(client):
    workflow = make_workflow(client, "deps")
    assert make_shell_task(client, workflow["id"], "extract").status_code == 201

    unknown = make_shell_task(client, workflow["id"], "transform", ["does-not-exist"])
    assert unknown.status_code == 422
    assert "Unknown task dependencies" in unknown.json()["detail"]

    self_dep = make_shell_task(client, workflow["id"], "loop", ["loop"])
    assert self_dep.status_code == 422

    transform = make_shell_task(client, workflow["id"], "transform", ["extract"])
    assert transform.status_code == 201
    extract_id = client.get(f"/api/workflows/{workflow['id']}/tasks").json()[0]["id"]
    cycle = client.put(f"/api/tasks/{extract_id}", json={
        "name": "extract", "task_type": "shell", "command": "echo ok",
        "depends_on_json": ["transform"], "retries": 0, "retry_delay_seconds": 0,
    })
    assert cycle.status_code == 422
    assert "cycle" in cycle.json()["detail"]


def test_task_rename_and_delete_keep_sibling_dependencies_consistent(client):
    workflow = make_workflow(client, "rename")
    extract = make_shell_task(client, workflow["id"], "extract").json()
    make_shell_task(client, workflow["id"], "transform", ["extract"])

    client.put(f"/api/tasks/{extract['id']}", json={
        "name": "extract-v2", "task_type": "shell", "command": "echo ok",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    tasks = {t["name"]: t for t in client.get(f"/api/workflows/{workflow['id']}/tasks").json()}
    assert tasks["transform"]["depends_on_json"] == ["extract-v2"]

    client.delete(f"/api/tasks/{tasks['extract-v2']['id']}")
    tasks = {t["name"]: t for t in client.get(f"/api/workflows/{workflow['id']}/tasks").json()}
    assert tasks["transform"]["depends_on_json"] == []


def test_workflow_without_tasks_cannot_be_run(client):
    workflow = make_workflow(client, "empty")
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    assert run.status_code == 409
    assert "at least one task" in run.json()["detail"]


def test_queued_run_can_be_cancelled(client):
    workflow = make_workflow(client, "cancellable")
    make_shell_task(client, workflow["id"], "hello")
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    cancelled = client.post(f"/api/runs/{run['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["finished_at"] is not None

    # Cancelled runs are never claimed by the worker.
    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    with SessionLocal() as db:
        assert claim_next_run(db) is None

    # A second cancel is rejected.
    assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 409


def test_worker_honours_cancellation_and_does_not_overwrite_it(client):
    workflow = make_workflow(client, "cancel-mid-run")
    make_shell_task(client, workflow["id"], "first")
    make_shell_task(client, workflow["id"], "second", ["first"])
    created = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run

    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None and run.id == created["id"]
        # The user cancels while the run is executing.
        assert client.post(f"/api/runs/{run.id}/cancel").status_code == 200
        execute_workflow_run(db, run)

    detail = client.get(f"/api/runs/{created['id']}").json()
    assert detail["status"] == "cancelled"
    assert detail["finished_at"] is not None
    assert all(t["status"] == "cancelled" for t in detail["task_runs"])


def test_api_timestamps_are_timezone_aware_utc(client):
    workflow = make_workflow(client, "tz")
    make_shell_task(client, workflow["id"], "hello")
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()
    # Naive SQLite datetimes must be tagged as UTC so browsers parse them correctly.
    assert run["created_at"].endswith("Z") or "+00:00" in run["created_at"]
    assert workflow["created_at"].endswith("Z") or "+00:00" in workflow["created_at"]


def test_run_detail_exposes_task_names(client):
    workflow = make_workflow(client, "named-tasks")
    make_shell_task(client, workflow["id"], "greet", command="printf hi")
    created = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run
    with SessionLocal() as db:
        run = claim_next_run(db)
        execute_workflow_run(db, run)

    detail = client.get(f"/api/runs/{created['id']}").json()
    assert detail["status"] == "success"
    assert detail["task_runs"][0]["task_name"] == "greet"
    assert detail["task_runs"][0]["task_type"] == "shell"
