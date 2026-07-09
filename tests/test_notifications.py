"""Trust package: transition-based webhook notifications, auto-pause, and retry."""


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1, **extra,
    }).json()


def make_shell_task(client, workflow_id, name, command):
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


def capture_webhooks(monkeypatch):
    sent = []
    monkeypatch.setattr("runrail.notify._post",
                        lambda url, text, **fields: sent.append({"url": url, "text": text, **fields}))
    return sent


def test_failure_notifies_on_transition_only_and_recovery(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "alerts", notify_webhook_url="https://hooks.example/x")
    task = make_shell_task(client, workflow["id"], "job", "exit 1")

    for _ in range(2):  # two consecutive failures -> exactly one alert
        client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
        execute_queued_run(client)
    assert [n["event"] for n in sent] == ["run_failed"]
    assert sent[0]["url"] == "https://hooks.example/x"

    # Fix the task; the next success sends a recovery message.
    client.put(f"/api/tasks/{task['id']}", json={
        "name": "job", "task_type": "shell", "command": "printf ok",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    execute_queued_run(client)
    assert [n["event"] for n in sent] == ["run_failed", "run_recovered"]

    # A steady-state success stays quiet.
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    execute_queued_run(client)
    assert len(sent) == 2


def test_auto_pause_disables_workflow_after_consecutive_failures(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "flaky", auto_pause_failures=2,
                             notify_webhook_url="https://hooks.example/x")
    make_shell_task(client, workflow["id"], "job", "exit 1")

    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    execute_queued_run(client)
    assert client.get(f"/api/workflows/{workflow['id']}").json()["enabled"] is True

    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    execute_queued_run(client)
    assert client.get(f"/api/workflows/{workflow['id']}").json()["enabled"] is False
    assert [n["event"] for n in sent] == ["run_failed", "workflow_paused"]


def test_retry_queues_new_run_with_same_parameters(client):
    workflow = make_workflow(client, "retryable")
    make_shell_task(client, workflow["id"], "job", "exit 1")
    created = client.post(f"/api/workflows/{workflow['id']}/run",
                          json={"parameters": {"region": "ca"}}).json()

    # A queued/running run cannot be retried.
    assert client.post(f"/api/runs/{created['id']}/retry").status_code == 409

    execute_queued_run(client)
    retried = client.post(f"/api/runs/{created['id']}/retry")
    assert retried.status_code == 201
    body = retried.json()
    assert body["id"] != created["id"]
    assert body["status"] == "queued"
    assert body["parameters_json"] == {"region": "ca"}
    assert body["trigger_type"] == "manual"
