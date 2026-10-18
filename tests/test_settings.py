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
                                             "notify_webhook_url": "https://hooks.example/x/"}
                      ).status_code == 200
    assert setting(client, "worker_concurrency")["source"] == "saved"
    assert setting(client, "worker_concurrency")["value"] == 8
    assert setting(client, "notify_webhook_url")["value"] == "https://hooks.example/x"
    assert runtime_settings.current("worker_concurrency") == 8


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
    ({"retention_days": 0}, "Keep runs for"),
    ({"quiet_hours": {"start": "22:00", "end": "07:00", "timezone": 5}}, "Quiet hours"),
    ({"notify_webhook_url": "hooks.example"}, "Default webhook"),
    ({"notify_webhook_url": "https://"}, "Default webhook"),
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
    # Delivery runs on a thread; for the assertion it runs inline.
    monkeypatch.setattr(notify.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: target(*args)})())
    monkeypatch.setattr(notify, "_send_and_record", lambda url, text, wid, f: sent.append(f.get("event")))
    client.put("/api/settings", json={"notify_events": ["run_failed", "run_recovered"]})
    notify._post("https://hooks.example/x", "x", event="run_failed")
    notify._post("https://hooks.example/x", "x", event="approval_requested")
    notify._post("https://hooks.example/x", "x")  # a test message has no kind
    assert sent == ["run_failed", None]
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
    # Saved without the API (which would also start a cleanup of its own), so
    # this is the scheduler's pass reading the setting.
    with SessionLocal() as db:
        runtime_settings.save(db, {"retention_days": 30})
    SchedulerService._cleanup()
    assert len(client.get("/api/runs?limit=10").json()) == 1


def test_pausing_stops_new_claims_and_resuming_releases_them(client, monkeypatch):
    """The worker loop itself: with Settings paused, a queued run is not
    claimed; the moment it is resumed, it is."""
    import threading

    from runrail.worker import service as worker_module
    from runrail.worker.service import WorkerService
    polls = []
    real_claim = worker_module.claim_runnable_run
    monkeypatch.setattr(worker_module, "claim_runnable_run",
                        lambda db: polls.append(1) or real_claim(db))
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
        assert thread.is_alive() and not polls, "the loop claimed while paused"
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"
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


def test_the_watchdogs_hold_their_tongue_while_paused(client, monkeypatch):
    """A pause for maintenance is not a missed schedule: the watchdog that
    would page about every quiet workflow waits for the resume."""
    from runrail.scheduler import service as scheduler
    calls = []
    monkeypatch.setattr(scheduler, "check_missed_runs", lambda db: calls.append("missed"))
    monkeypatch.setattr(scheduler, "check_sla_breaches", lambda db: calls.append("sla"))
    client.post("/api/system/pause", json={"paused": True})
    with SessionLocal() as db:
        scheduler.check_watchdogs(db)
    assert calls == []
    client.post("/api/system/pause", json={"paused": False})
    with SessionLocal() as db:
        scheduler.check_watchdogs(db)
    assert calls == ["missed", "sla"]


def test_an_environment_figure_above_the_settings_range_still_gets_its_pool(client, monkeypatch):
    from runrail.worker.service import _POOL_CAP, WorkerService
    monkeypatch.setenv("RUNRAIL_WORKER_CONCURRENCY", "100")
    get_settings.cache_clear(); runtime_settings.invalidate()
    worker = WorkerService()
    assert worker.concurrency == 100 and _POOL_CAP < 100
    # The pool is sized to the figure the loop will claim up to.
    assert max(_POOL_CAP, worker.concurrency) == 100


def test_a_zero_in_the_environment_means_never_like_the_cleanup_says(client, monkeypatch):
    monkeypatch.setenv("RUNRAIL_RETENTION_DAYS", "0")
    get_settings.cache_clear(); runtime_settings.invalidate()
    shown = setting(client, "retention_days")
    assert shown["locked"] and shown["value"] is None


def test_an_empty_environment_value_is_not_a_pin(client, monkeypatch):
    monkeypatch.setenv("RUNRAIL_NOTIFY_WEBHOOK_URL", "")
    get_settings.cache_clear(); runtime_settings.invalidate()
    assert setting(client, "notify_webhook_url")["locked"] is False


def test_state_changing_requests_from_another_site_are_refused(client):
    """No accounts, so the browser is the perimeter: a page on another site
    must not be able to POST to this RunRail. Same-origin, loopback-to-loopback
    (the dev proxy) and header-less (curl) requests go through."""
    body = {"paused": True}
    assert client.post("/api/system/pause", json=body,
                       headers={"Origin": "https://evil.example", "Host": "127.0.0.1:8080"}
                       ).status_code == 403
    assert client.post("/api/system/pause", json=body,
                       headers={"Origin": "null", "Host": "127.0.0.1:8080"}).status_code == 403
    assert client.post("/api/system/pause", json={"paused": False},
                       headers={"Origin": "http://127.0.0.1:8080", "Host": "127.0.0.1:8080"}
                       ).status_code == 200
    assert client.post("/api/system/pause", json={"paused": False},
                       headers={"Origin": "http://localhost:5173", "Host": "127.0.0.1:8080"}
                       ).status_code == 200
    assert client.get("/api/system/status", headers={"Origin": "https://evil.example"}
                      ).status_code == 200, "reads are protected by the browser itself (no CORS)"
    assert client.post("/api/system/pause", json={"paused": False}).status_code == 200


def test_saving_a_retention_figure_applies_it_now(client):
    """Waiting for the scheduler's pass, hours away, looked like the setting
    had done nothing: the preview kept saying what it would delete."""
    from runrail import system
    workflow = make_workflow(client, "old")
    with SessionLocal() as db:
        for days_ago in (40, 35, 1):
            db.add(WorkflowRun(workflow_id=workflow["id"], status=RunStatus.failed, trigger_type="manual",
                               created_at=datetime.now(UTC) - timedelta(days=days_ago),
                               finished_at=datetime.now(UTC) - timedelta(days=days_ago)))
        db.commit()
    assert client.get("/api/system/retention-preview?days=30").json()["runs"] == 2
    assert client.put("/api/settings", json={"retention_days": 30}).status_code == 200
    deadline = time.monotonic() + 10
    while client.get("/api/system/status").json()["retention"]["cleanup"]["running"]:
        assert time.monotonic() < deadline, "the cleanup never finished"
        time.sleep(0.05)
    done = client.get("/api/system/status").json()["retention"]["cleanup"]
    assert done["runs_deleted"] == 2 and done["days"] == 30 and done["finished_at"]
    # On record, so a restart (a fresh process, nothing in memory) still says so.
    remembered = system.cleanup.state
    system.cleanup.state = {**remembered, "started_at": None, "finished_at": None}
    last = client.get("/api/system/status").json()["retention"]["last"]
    assert last["runs_deleted"] == 2 and last["days"] == 30 and last["finished_at"]
    system.cleanup.state = remembered
    assert client.get("/api/system/retention-preview?days=30").json()["runs"] == 0
    assert len(client.get("/api/runs?limit=10").json()) == 1
    # Saving something else does not start another.
    client.put("/api/settings", json={"task_parallelism": 3})
    assert client.get("/api/system/status").json()["retention"]["cleanup"]["started_at"] == done["started_at"]
    # Only one at a time, whoever asks.
    assert system.cleanup._claim(30)
    assert system.cleanup.start(30) is False and system.cleanup.run(30, claimed=False) is None
    system.cleanup.state["running"] = False
