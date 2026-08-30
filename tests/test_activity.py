"""The derived activity feed: one event per thing that actually happened, told
the same way notify.py tells it to a webhook."""

from datetime import timedelta

from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun, now

HOOK = "https://hooks.example/x"


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
        "notify_webhook_url": HOOK, **extra,
    }).json()


def make_shell_task(client, workflow_id, name, command, **extra):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0, **extra,
    }).json()


def execute_queued_run(client):
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run
    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None
        execute_workflow_run(db, run)
        return run.id


def run_once(client, workflow_id):
    client.post(f"/api/workflows/{workflow_id}/run", json={"parameters": {}})
    return execute_queued_run(client)


def capture_webhooks(monkeypatch):
    sent = []
    monkeypatch.setattr("runrail.notify._post",
                        lambda url, text, **fields: sent.append({"url": url, "text": text, **fields}))
    return sent


def configure(workflow_id: int, *, aged_minutes: int = 0, **values):
    """updated_at is written explicitly: it anchors the missed-run watchdog."""
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        for key, value in values.items():
            setattr(workflow, key, value)
        workflow.updated_at = now() - timedelta(minutes=aged_minutes)
        db.commit()


def add_run(workflow_id, status, *, age_minutes=0, trigger=TriggerType.schedule) -> int:
    with SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow_id, status=status, trigger_type=trigger,
                          created_at=now() - timedelta(minutes=age_minutes))
        db.add(run); db.commit()
        return run.id


def age_run(run_id: int, days: int) -> None:
    """Move both stamps: created_at is what the window filters on, finished_at is
    what the event is dated by."""
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        run.created_at = now() - timedelta(days=days)
        run.finished_at = now() - timedelta(days=days)
        db.commit()


def run_watchdogs() -> None:
    from runrail.scheduler.service import check_watchdogs
    with SessionLocal() as db:
        check_watchdogs(db)


def feed(client, **params):
    response = client.get("/api/activity", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def kinds(body):
    return [event["kind"] for event in body["events"]]


def test_an_empty_install_returns_an_empty_feed(client):
    body = feed(client)
    assert body["events"] == [] and body["total"] == 0 and body["unread"] == 0

    # A workflow that has never run is not an event either.
    make_workflow(client, "brand-new")
    assert feed(client)["events"] == []


def test_failure_transition_and_recovery_match_the_webhooks(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "alerts")
    task = make_shell_task(client, workflow["id"], "job", "exit 1")

    for _ in range(2):  # two consecutive failures are one incident
        failed_run = run_once(client, workflow["id"])
    body = feed(client)
    assert kinds(body) == ["run_failed"]
    event = body["events"][0]
    assert event["severity"] == "error"
    assert event["workflow_id"] == workflow["id"] and event["workflow_name"] == "alerts"
    # The first failure is the incident; the second red run is not a new one.
    assert event["run_id"] != failed_run

    client.put(f"/api/tasks/{task['id']}", json={
        "name": "job", "task_type": "shell", "command": "printf ok",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0})
    recovered_run = run_once(client, workflow["id"])
    run_once(client, workflow["id"])  # steady-state success adds nothing

    body = feed(client)
    assert kinds(body) == ["run_recovered", "run_failed"]  # newest first
    assert body["events"][0]["severity"] == "success"
    assert body["events"][0]["run_id"] == recovered_run
    # The whole point: the bell and the webhook cannot tell different stories.
    assert kinds(body)[::-1] == [alert["event"] for alert in sent]


def test_a_resumed_run_does_not_re_announce_its_failure(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "resumable")
    make_shell_task(client, workflow["id"], "job", "exit 1")
    run_id = run_once(client, workflow["id"])
    assert kinds(feed(client)) == ["run_failed"]

    client.post(f"/api/runs/{run_id}/resume")
    execute_queued_run(client)
    # One logical failure, one alert, one event. notify.py needs its resume_count
    # guard to get there; the feed gets there for free, because the run it is
    # derived from is a single row however many times it is resumed.
    assert [alert["event"] for alert in sent] == ["run_failed"]
    body = feed(client)
    assert kinds(body) == ["run_failed"] and body["events"][0]["run_id"] == run_id


def test_auto_pause_names_the_workflow_and_the_run_that_tripped_it(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "flaky", auto_pause_failures=2)
    make_shell_task(client, workflow["id"], "job", "exit 1")
    run_once(client, workflow["id"])
    tripping_run = run_once(client, workflow["id"])

    assert client.get(f"/api/workflows/{workflow['id']}").json()["enabled"] is False
    body = feed(client)
    assert kinds(body) == ["workflow_paused", "run_failed"]
    paused = body["events"][0]
    assert paused["severity"] == "error"
    assert paused["workflow_id"] == workflow["id"] and paused["run_id"] == tripping_run
    assert kinds(body)[::-1] == [alert["event"] for alert in sent]

    # Re-enabling clears it: the pause is state, not history, so the feed stops
    # claiming a workflow is paused the moment it is not.
    configure(workflow["id"], enabled=True)
    assert kinds(feed(client)) == ["run_failed"]


def test_an_open_gate_is_an_event_pointing_at_its_run(client):
    workflow = make_workflow(client, "needs-a-human")
    make_shell_task(client, workflow["id"], "publish", "printf published",
                    requires_approval=True, approval_prompt="Row counts right?")
    run_id = run_once(client, workflow["id"])
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "waiting_approval"

    body = feed(client)
    assert kinds(body) == ["approval_requested"]
    event = body["events"][0]
    assert event["severity"] == "info"
    assert event["run_id"] == run_id and event["task_name"] == "publish"

    # Deciding the gate is what closes the ask, so the event goes with it.
    gate = client.get("/api/approvals").json()[0]
    approved = client.post(f"/api/task-runs/{gate['task_run_id']}/approve",
                           json={"approved_by": "me"})
    assert approved.status_code == 200, approved.text
    assert feed(client)["events"] == []


def test_watchdog_markers_surface_as_sla_and_missed_events(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    late = make_workflow(client, "deadline", sla_minutes=60)
    breaching = add_run(late["id"], RunStatus.running, age_minutes=100,
                        trigger=TriggerType.manual)
    silent = make_workflow(client, "nightly", schedule_cron="*/5 * * * *",
                           missed_run_grace_minutes=10)
    add_run(silent["id"], RunStatus.success, age_minutes=90)
    configure(silent["id"], aged_minutes=120)

    run_watchdogs()
    assert sorted(alert["event"] for alert in sent) == ["run_missed", "sla_breached"]

    by_kind = {event["kind"]: event for event in feed(client)["events"]}
    assert set(by_kind) == {"sla_breached", "run_missed"}
    assert by_kind["sla_breached"]["severity"] == "warning"
    assert by_kind["sla_breached"]["run_id"] == breaching
    assert by_kind["sla_breached"]["workflow_id"] == late["id"]
    assert by_kind["run_missed"]["severity"] == "warning"
    assert by_kind["run_missed"]["workflow_id"] == silent["id"]
    # A workflow-level event has no run to link to; the UI links the workflow.
    assert by_kind["run_missed"]["run_id"] is None

    # Recovery clears the marker, so the feed stops saying the schedule is dead.
    add_run(silent["id"], RunStatus.success)
    run_watchdogs()
    assert "run_missed" not in kinds(feed(client))


def test_a_snoozed_workflow_is_muted_and_comes_back_when_the_mute_lifts(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "noisy", auto_pause_failures=2)
    make_shell_task(client, workflow["id"], "job", "exit 1")
    client.post(f"/api/workflows/{workflow['id']}/snooze",
                json={"until": (now() + timedelta(hours=2)).isoformat()})

    for _ in range(2):
        run_once(client, workflow["id"])
    # notify.py's real behaviour, asserted rather than assumed: snooze mutes the
    # talking, not the doing — nothing was posted, but the workflow did pause.
    assert sent == []
    assert client.get(f"/api/workflows/{workflow['id']}").json()["enabled"] is False
    body = feed(client)
    assert body["events"] == [] and body["total"] == 0 and body["unread"] == 0

    # A mute, not a delete: snooze expires by the clock, and what it silenced is
    # still there to be read afterwards.
    client.delete(f"/api/workflows/{workflow['id']}/snooze")
    assert kinds(feed(client)) == ["workflow_paused", "run_failed"]


def test_the_limit_and_the_window_bound_the_feed(client):
    workflow = make_workflow(client, "history")
    make_shell_task(client, workflow["id"], "job", "exit 1")
    old_failure = run_once(client, workflow["id"])
    age_run(old_failure, days=10)
    make_shell_task(client, workflow["id"], "second", "exit 1")

    fresh = make_workflow(client, "fresh")
    make_shell_task(client, fresh["id"], "job", "exit 1")
    run_once(client, fresh["id"])
    add_run(fresh["id"], RunStatus.running, age_minutes=5)
    configure(fresh["id"], sla_minutes=1)
    run_watchdogs()

    body = feed(client, window_hours=24 * 30)
    assert body["total"] == 3  # both failures plus the breach
    assert len(body["events"]) == 3

    # The window is a cutoff, not a hint: the ten-day-old failure is gone, and
    # `total` counts what is in the window rather than what exists.
    windowed = feed(client, window_hours=24)
    assert windowed["total"] == 2
    assert old_failure not in [event["run_id"] for event in windowed["events"]]

    # The limit truncates the page; the counts still describe the whole window,
    # which is what lets the badge avoid fetching everything.
    capped = feed(client, window_hours=24, limit=1)
    assert len(capped["events"]) == 1 and capped["total"] == 2 and capped["unread"] == 2
    assert capped["events"][0] == windowed["events"][0]  # newest first, page one


def test_unread_counts_what_is_newer_than_the_clients_last_read(client):
    workflow = make_workflow(client, "bell")
    make_shell_task(client, workflow["id"], "job", "exit 1")
    run_once(client, workflow["id"])

    body = feed(client)
    assert body["total"] == 1 and body["unread"] == 1  # no last-read: all unread

    # The client stamps generated_at when the panel is opened; nothing is newer.
    assert feed(client, read_at=body["generated_at"])["unread"] == 0
    assert feed(client, read_at=(now() - timedelta(days=1)).isoformat())["unread"] == 1

    # Read state is never written server-side, so a second reader with an older
    # stamp still sees it as unread.
    assert feed(client, read_at=(now() - timedelta(days=1)).isoformat())["unread"] == 1
