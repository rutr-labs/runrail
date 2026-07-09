"""Workflow-as-code: YAML export/apply round-trips and declarative task upserts."""

import pytest


def seed_workflow(client):
    workflow = client.post("/api/workflows", json={
        "name": "etl", "enabled": True, "max_concurrent_runs": 2,
        "schedule_cron": "*/5 * * * *", "notify_webhook_url": "https://hooks.example/x",
        "auto_pause_failures": 3,
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "extract", "task_type": "shell", "command": "printf extract",
        "depends_on_json": [], "retries": 1, "retry_delay_seconds": 5,
        "parameters_json": {"region": "ca"},
    })
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "load", "task_type": "shell", "command": "printf load",
        "depends_on_json": ["extract"], "retries": 0, "retry_delay_seconds": 0,
    })
    return workflow


def test_export_apply_round_trip_is_idempotent(client):
    from runrail.db import SessionLocal
    from runrail.workflow_io import apply_workflows, export_workflows

    seed_workflow(client)
    with SessionLocal() as db:
        data = export_workflows(db)
    exported = data["workflows"][0]
    assert exported["name"] == "etl"
    assert exported["schedule_cron"] == "*/5 * * * *"
    assert exported["auto_pause_failures"] == 3
    assert [t["name"] for t in exported["tasks"]] == ["extract", "load"]
    assert exported["tasks"][0]["parameters"] == {"region": "ca"}
    assert exported["tasks"][1]["depends_on"] == ["extract"]

    with SessionLocal() as db:
        summary = apply_workflows(db, data)
    assert summary == {"created": [], "updated": ["etl"]}
    tasks = client.get(f"/api/workflows/{client.get('/api/workflows').json()[0]['id']}/tasks").json()
    assert [t["name"] for t in tasks] == ["extract", "load"]  # no duplicates


def test_apply_updates_removes_and_creates_declaratively(client):
    from runrail.db import SessionLocal
    from runrail.workflow_io import apply_workflows, export_workflows

    workflow = seed_workflow(client)
    with SessionLocal() as db:
        data = export_workflows(db)
    spec = data["workflows"][0]
    spec["tasks"][0]["command"] = "printf changed"          # update
    spec["tasks"] = [spec["tasks"][0], {                     # drop 'load', add 'report'
        "name": "report", "task_type": "shell", "command": "printf report",
        "depends_on": ["extract"],
    }]
    data["workflows"].append({                               # brand-new workflow
        "name": "fresh", "enabled": False,
        "tasks": [{"name": "solo", "task_type": "shell", "command": "printf hi"}],
    })

    with SessionLocal() as db:
        summary = apply_workflows(db, data)
    assert summary == {"created": ["fresh"], "updated": ["etl"]}

    tasks = client.get(f"/api/workflows/{workflow['id']}/tasks").json()
    assert {t["name"]: t.get("command") for t in tasks} == {
        "extract": "printf changed", "report": "printf report",
    }
    flows = {w["name"]: w for w in client.get("/api/workflows").json()}
    assert flows["fresh"]["enabled"] is False


def test_apply_rejects_unknown_references_and_cycles(client):
    from runrail.db import SessionLocal
    from runrail.workflow_io import apply_workflows

    with SessionLocal() as db:
        with pytest.raises(ValueError, match="Unknown environment"):
            apply_workflows(db, {"workflows": [{
                "name": "bad-env", "default_environment": "nope", "tasks": [],
            }]})
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="cycle"):
            apply_workflows(db, {"workflows": [{
                "name": "bad-cycle", "tasks": [
                    {"name": "a", "task_type": "shell", "command": "x", "depends_on": ["b"]},
                    {"name": "b", "task_type": "shell", "command": "x", "depends_on": ["a"]},
                ],
            }]})
    # Failed applies must not leave partial workflows behind.
    assert client.get("/api/workflows").json() == []
