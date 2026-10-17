"""Alerts decided once, at the source.

The failure/recovery transition, the auto-pause and the feed's view of them
were reconstructed from run ids after the fact, and disagreed with what had
happened whenever a run was resumed, two finished together, or a worker died
mid-run. Each test here is one of those cases.
"""

from datetime import timedelta

from portable import emit
from test_watchdogs import (
    add_run,
    capture_webhooks,
    configure,
    run_watchdogs,
    workflow_state,
)

from runrail import notify
from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, WorkflowRun, now
from runrail.worker.queue import claim_next_run
from runrail.worker.service import execute_workflow_run, recover_interrupted_runs


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
        "notify_webhook_url": "https://hooks.example/x", **extra}).json()


def make_task(client, workflow_id, command):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": "job", "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0}).json()


def set_command(client, task_id, command):
    assert client.put(f"/api/tasks/{task_id}", json={
        "name": "job", "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0}).status_code == 200


def run_once(client, workflow_id) -> int:
    client.post(f"/api/workflows/{workflow_id}/run", json={"parameters": {}})
    with SessionLocal() as db:
        run = claim_next_run(db)
        execute_workflow_run(db, run)
        return run.id


def events(sent):
    return [n["event"] for n in sent]


def feed(client):
    return [e["kind"] for e in client.get("/api/activity").json()["events"]]


def test_a_failed_run_resumed_to_success_is_the_recovery(client, monkeypatch):
    """The main fix path: fix the task, resume the failed run. The comparison
    against "the newest other run" found the older success and said nothing,
    so the failure alert was never closed."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "etl")
    task = make_task(client, workflow["id"], emit("ok"))
    run_once(client, workflow["id"])
    set_command(client, task["id"], "exit 1")
    failed = run_once(client, workflow["id"])
    assert events(sent) == ["run_failed"]

    set_command(client, task["id"], emit("fixed"))
    assert client.post(f"/api/runs/{failed}/resume", json={}).status_code == 200
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    assert events(sent) == ["run_failed", "run_recovered"]
    assert sent[-1]["run_id"] == failed
    # One run, one row: the feed shows the run's latest alert.
    assert feed(client) == ["run_recovered"]


def test_a_rejected_run_resumed_and_then_failing_still_alerts(client, monkeypatch):
    """A guard assumed every resumed run had alerted once already. A run
    rejected at a gate (cancelled, no alert) and resumed into a failure had not."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "gated")
    task = make_task(client, workflow["id"], "exit 1")
    client.put(f"/api/tasks/{task['id']}", json={
        "name": "job", "task_type": "shell", "command": "exit 1", "requires_approval": True,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0})
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    gate = client.get("/api/approvals").json()[0]
    assert client.post(f"/api/task-runs/{gate['task_run_id']}/reject", json={}).status_code == 200
    assert events(sent) == ["approval_requested", "approval_rejected"]
    run_id = gate["run_id"]
    with SessionLocal() as db:  # the rejection re-enters the run, which lands cancelled
        execute_workflow_run(db, claim_next_run(db))
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "cancelled"
    assert client.post(f"/api/runs/{run_id}/resume", json={}).status_code == 200
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    gate = client.get("/api/approvals").json()[0]
    client.post(f"/api/task-runs/{gate['task_run_id']}/approve", json={})
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    assert events(sent)[-1] == "run_failed"


def test_two_runs_finishing_together_send_one_alert(client, monkeypatch):
    """Both saw the other as "previous outcome: failed" and neither alerted."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "parallel", max_concurrent_runs=2)
    make_task(client, workflow["id"], "exit 1")
    with SessionLocal() as db:
        runs = [WorkflowRun(workflow_id=workflow["id"], status=RunStatus.failed,
                            trigger_type=TriggerType.manual, finished_at=now()) for _ in range(2)]
        db.add_all(runs); db.commit()
        for run in runs:  # the two finalizations, back to back
            notify.notify_run_outcome(db, run)
    assert events(sent) == ["run_failed"]


def test_a_run_interrupted_by_a_worker_crash_alerts_like_any_failure(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "crashed", auto_pause_failures=1)
    make_task(client, workflow["id"], emit("ok"))
    with SessionLocal() as db:
        db.add(WorkflowRun(workflow_id=workflow["id"], status=RunStatus.running,
                           trigger_type=TriggerType.schedule, started_at=now()))
        db.commit()
    with SessionLocal() as db:
        assert recover_interrupted_runs(db) == 1
    assert events(sent) == ["run_failed", "workflow_paused"]
    assert workflow_state(workflow["id"]).enabled is False


def test_re_enabling_starts_the_auto_pause_count_afresh(client, monkeypatch):
    """The streak was never anchored to the re-enable, so the next failure
    after switching a workflow back on paused it again — "after 3 consecutive
    failures", on the strength of one."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "flaky", auto_pause_failures=2)
    task = make_task(client, workflow["id"], "exit 1")
    run_once(client, workflow["id"]); run_once(client, workflow["id"])
    assert events(sent) == ["run_failed", "workflow_paused"]
    assert workflow_state(workflow["id"]).enabled is False
    assert feed(client)[0] == "workflow_paused"

    assert client.put(f"/api/workflows/{workflow['id']}", json={"name": "flaky", "enabled": True}).status_code == 200
    assert "workflow_paused" not in feed(client)
    run_once(client, workflow["id"])  # one failure after the re-enable
    assert workflow_state(workflow["id"]).enabled is True, "paused again after one failure"
    run_once(client, workflow["id"])
    assert workflow_state(workflow["id"]).enabled is False
    set_command(client, task["id"], emit("ok"))


def test_the_pause_is_committed_before_anything_is_posted(client, monkeypatch):
    """With the webhook posted first, the queued iteration could be claimed
    during the post and run once more after "paused"."""
    seen = []

    def post(url, text, **fields):
        seen.append((fields["event"], workflow_state(workflow["id"]).enabled))

    monkeypatch.setattr(notify, "_post", post)
    workflow = make_workflow(client, "brake", auto_pause_failures=1)
    make_task(client, workflow["id"], "exit 1")
    run_once(client, workflow["id"])
    assert seen == [("run_failed", False), ("workflow_paused", False)]


def test_a_resumed_run_measures_its_sla_from_the_resume(client, monkeypatch):
    """Yesterday's run resumed today was twenty-three hours late the moment it
    started, and its late-finish message said so."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "sla", sla_minutes=30)
    make_task(client, workflow["id"], emit("ok"))
    with SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow["id"], status=RunStatus.failed,
                          trigger_type=TriggerType.schedule,
                          created_at=now() - timedelta(days=1), finished_at=now() - timedelta(days=1))
        db.add(run); db.commit(); run_id = run.id
    assert client.post(f"/api/runs/{run_id}/resume", json={}).status_code == 200
    run_watchdogs()
    assert events(sent) == [], "breached a 30-minute deadline within a second of resuming"
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    assert "sla_finished_late" not in events(sent)


def test_a_manual_run_does_not_end_a_missed_schedule(client, monkeypatch):
    """Any run used to count as a recovery, after which the miss alerted again
    a minute later — and so on for every manual run."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "nightly", schedule_cron="*/5 * * * *",
                             missed_run_grace_minutes=10)
    add_run(workflow["id"], RunStatus.success, age_minutes=90)
    configure(workflow["id"], aged_minutes=120)
    run_watchdogs()
    assert events(sent) == ["run_missed"]
    add_run(workflow["id"], RunStatus.success, trigger=TriggerType.manual)
    run_watchdogs()
    assert events(sent) == ["run_missed"]
    # A paused workflow has not recovered either, whatever runs by hand.
    configure(workflow["id"], enabled=False)
    add_run(workflow["id"], RunStatus.success)
    run_watchdogs()
    assert events(sent) == ["run_missed"]
    configure(workflow["id"], enabled=True)
    run_watchdogs()
    assert events(sent) == ["run_missed", "run_missed_recovered"]


def test_removing_the_watch_clears_a_standing_miss(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "nightly", schedule_cron="*/5 * * * *",
                             missed_run_grace_minutes=10)
    add_run(workflow["id"], RunStatus.success, age_minutes=90)
    configure(workflow["id"], aged_minutes=120)
    run_watchdogs()
    assert events(sent) == ["run_missed"] and feed(client) == ["run_missed"]
    client.put(f"/api/workflows/{workflow['id']}", json={"name": "nightly", "missed_run_grace_minutes": None})
    assert workflow_state(workflow["id"]).missed_notified_at is None
    assert feed(client) == []


def test_a_failed_delivery_is_recorded_and_a_test_post_reports_it(client, monkeypatch):
    monkeypatch.setattr(notify, "deliver", lambda url, text, **fields: "HTTP 404 Not Found")
    workflow = make_workflow(client, "broken")
    result = client.post(f"/api/workflows/{workflow['id']}/notify-test").json()
    assert result == {"ok": False, "error": "HTTP 404 Not Found", "url_host": "hooks.example"}
    state = client.get(f"/api/workflows/{workflow['id']}").json()
    assert state["webhook_error"] == "HTTP 404 Not Found" and state["webhook_error_at"]
    monkeypatch.setattr(notify, "deliver", lambda url, text, **fields: None)
    assert client.post(f"/api/workflows/{workflow['id']}/notify-test").json()["ok"] is True
    assert client.get(f"/api/workflows/{workflow['id']}").json()["webhook_error"] is None
    no_hook = make_workflow(client, "silent", notify_webhook_url=None)
    assert client.post(f"/api/workflows/{no_hook['id']}/notify-test").status_code == 400

