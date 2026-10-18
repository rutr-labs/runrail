"""Settings changed from the page: three layers (environment, saved, default),
validated before anything is written, and read by the worker, the scheduler
and the alerts without a restart."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from portable import emit
from test_interactions import make_shell_task, make_workflow, start_run

from runrail import notify, runtime_settings
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.models import RunStatus, WorkflowRun
from runrail.worker.queue import claim_next_run


def setting(client, key):
    return next(s for s in client.get("/api/settings").json()["settings"] if s["key"] == key)


def test_defaults_then_saved_values_with_their_source(client):
    assert setting(client, "worker_concurrency") == pytest.approx(
        {"key": "worker_concurrency", "value": 4, "default": 4, "source": "default",
         "locked": False, "env_var": "RUNRAIL_WORKER_CONCURRENCY", "updated_at": None,
         "title": "Concurrent runs"} | {"help": setting(client, "worker_concurrency")["help"]})
    assert client.put("/api/settings", json={"worker_concurrency": 8,
                                             "public_url": "https://rail.example/"}).status_code == 200
    assert setting(client, "worker_concurrency")["source"] == "saved"
    assert setting(client, "worker_concurrency")["value"] == 8
    assert setting(client, "public_url")["value"] == "https://rail.example"
    assert runtime_settings.current("worker_concurrency") == 8
    assert runtime_settings.base_url() == "https://rail.example"


def test_an_environment_variable_pins_a_setting(client, monkeypatch):
    """A deployment that sets RUNRAIL_X in its unit file must stay what the
    unit file says: the page shows it locked and refuses to change it."""
    monkeypatch.setenv("RUNRAIL_WORKER_CONCURRENCY", "2")
    get_settings.cache_clear()
    runtime_settings.invalidate()
    shown = setting(client, "worker_concurrency")
    assert shown["value"] == 2 and shown["source"] == "environment" and shown["locked"]
    refused = client.put("/api/settings", json={"worker_concurrency": 8})
    assert refused.status_code == 409 and "RUNRAIL_WORKER_CONCURRENCY" in refused.json()["detail"]
    assert runtime_settings.current("worker_concurrency") == 2


@pytest.mark.parametrize("body, wrong", [
    ({"worker_concurrency": 0}, "Concurrent runs"),
    ({"worker_concurrency": "4"}, "Concurrent runs"),
    ({"retention_days": -1}, "Keep runs for"),
    ({"public_url": "rail.example"}, "Public URL"),
    ({"notify_events": ["run_failed", "nope"]}, "Events to send"),
    ({"quiet_hours": {"start": "22:00", "end": "22:00", "timezone": "UTC"}}, "Quiet hours"),
    ({"quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "Mars/Olympus"}}, "Quiet hours"),
    ({"paused": "yes"}, "Paused"),
])
def test_invalid_values_are_refused_and_nothing_is_written(client, body, wrong):
    before = client.get("/api/settings").json()
    response = client.put("/api/settings", json={"task_parallelism": 2, **body})
    assert response.status_code == 422 and wrong in response.json()["detail"]
    assert client.get("/api/settings").json() == before, "a save is all or nothing"
    assert client.put("/api/settings", json={"nonsense": 1}).status_code == 404


def test_alert_kinds_can_be_switched_off_and_quiet_hours_mute_the_rest(client, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send_and_record", lambda url, text, wid, f: sent.append(f["event"]))
    client.put("/api/settings", json={"notify_events": ["run_failed", "run_recovered"]})
    notify._post("https://hooks.example/x", "x", event="run_failed")
    notify._post("https://hooks.example/x", "x", event="approval_requested")
    notify._post("https://hooks.example/x", "x")  # a test message has no kind
    assert sent == ["run_failed", None] or sent == ["run_failed"], sent
    sent.clear()
    client.put("/api/settings", json={"quiet_hours": {"start": "22:00", "end": "07:00",
                                                      "timezone": "Europe/London"}})
    # 23:30 London is inside; 09:00 London is outside; the span crosses midnight.
    assert not runtime_settings.alerts_allowed("run_failed", datetime(2026, 6, 1, 22, 30, tzinfo=UTC))
    assert runtime_settings.alerts_allowed("run_failed", datetime(2026, 6, 1, 8, 0, tzinfo=UTC))
    assert not runtime_settings.alerts_allowed("run_failed", datetime(2026, 6, 1, 5, 59, tzinfo=UTC))
    assert runtime_settings.alerts_allowed("run_failed", datetime(2026, 6, 1, 6, 0, tzinfo=UTC))


def test_the_default_webhook_comes_from_settings(client, monkeypatch):
    monkeypatch.setattr(notify, "deliver", lambda url, text, **f: None if "good" in url else "HTTP 404")
    assert client.post("/api/settings/notify-test").status_code == 400
    client.put("/api/settings", json={"notify_webhook_url": "https://hooks.example/good"})
    assert client.post("/api/settings/notify-test").json()["ok"] is True
    workflow = make_workflow(client, "w")
    with SessionLocal() as db:
        from runrail.models import Workflow
        assert notify.webhook_url(db.get(Workflow, workflow["id"])) == "https://hooks.example/good"


def test_retention_reads_the_saved_setting_without_a_restart(client):
    from runrail.scheduler.service import SchedulerService
    workflow = make_workflow(client, "old")
    with SessionLocal() as db:
        for days_ago in (40, 1):
            db.add(WorkflowRun(workflow_id=workflow["id"], status=RunStatus.failed,
                               trigger_type="manual",
                               created_at=datetime.now(UTC) - timedelta(days=days_ago),
                               finished_at=datetime.now(UTC) - timedelta(days=days_ago)))
        db.commit()
    SchedulerService._cleanup()  # retention unset: nothing happens
    assert len(client.get("/api/runs?limit=10").json()) == 2
    client.put("/api/settings", json={"retention_days": 30})
    SchedulerService._cleanup()
    assert len(client.get("/api/runs?limit=10").json()) == 1


def test_pausing_stops_new_claims_and_resuming_releases_them(client):
    """The worker loop itself: with Settings paused, a queued run is not
    claimed; the moment it is resumed, it is."""
    import threading

    from runrail.worker.service import WorkerService
    workflow = make_workflow(client, "p")
    make_shell_task(client, workflow["id"], "t", emit("hi"))
    client.put("/api/settings", json={"paused": True, "worker_concurrency": 1})
    run_id = start_run(client, workflow["id"])
    worker = WorkerService()
    assert worker.concurrency == 1
    thread = threading.Thread(target=worker.run, kwargs={"install_signals": False}, daemon=True)
    thread.start()
    try:
        time.sleep(1.5)
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued", "claimed while paused"
        assert client.get("/api/system/status").json()["worker"]["paused"] is True
        client.post("/api/system/pause", json={"paused": False})
        deadline = time.monotonic() + 15
        while client.get(f"/api/runs/{run_id}").json()["status"] != "success":
            assert time.monotonic() < deadline, "not claimed after resuming"
            time.sleep(0.2)
    finally:
        worker.stop()
        thread.join(timeout=20)
    with SessionLocal() as db:
        assert claim_next_run(db) is None
