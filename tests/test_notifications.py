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


def test_payload_shape_follows_receiver():
    from runrail.notify import _payload_for

    fields = {"event": "run_failed", "workflow": "etl", "run_id": 7}

    # Slack and generic receivers keep the flat text payload.
    for url in ("https://hooks.slack.com/services/T/B/x", "https://alerts.internal/hook"):
        payload = _payload_for(url, "boom", fields)
        assert payload["text"] == "boom" and payload["event"] == "run_failed"

    # Power Automate (Teams) gets the adaptive-card envelope its default
    # "when a webhook request is received" template requires.
    pa = "https://prod-77.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=x"
    payload = _payload_for(pa, "boom", fields)
    assert payload["type"] == "message"
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == "boom"
    facts = {f["title"]: f["value"] for f in card["body"][1]["facts"]}
    assert facts == {"Event": "run_failed", "Workflow": "etl", "Run Id": "7"}

    # Newer Power Platform endpoints and legacy connector URLs count as Teams too.
    assert _payload_for("https://x.api.powerplatform.com/flows/y", "t", {})["type"] == "message"
    assert _payload_for("https://contoso.webhook.office.com/webhookb2/z", "t", {})["type"] == "message"
