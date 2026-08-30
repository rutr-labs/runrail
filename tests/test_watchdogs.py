"""Snooze and the two schedule watchdogs: a muted workflow, a schedule that went
silent, and a run that blew its deadline."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun, now

HOOK = "https://hooks.example/x"


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
        "notify_webhook_url": HOOK, **extra,
    }).json()


def make_shell_task(client, workflow_id, name, command):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    }).json()


def execute_queued_run(client):
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


def configure(workflow_id: int, *, aged_minutes: int = 0, **values):
    """Write config and operator state straight onto the row.

    updated_at is always set explicitly: it anchors the missed-run check, so
    letting onupdate bump it would silently reset the very thing under test.
    """
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        for key, value in values.items():
            setattr(workflow, key, value)
        workflow.updated_at = now() - timedelta(minutes=aged_minutes)
        db.commit()


def add_run(workflow_id, status, *, age_minutes=0, trigger=TriggerType.schedule) -> int:
    """Runs are aged by writing created_at, never by sleeping."""
    with SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow_id, status=status, trigger_type=trigger,
                          created_at=now() - timedelta(minutes=age_minutes))
        db.add(run); db.commit()
        return run.id


def age_run(run_id: int, minutes: int) -> None:
    with SessionLocal() as db:
        db.get(WorkflowRun, run_id).created_at = now() - timedelta(minutes=minutes)
        db.commit()


def run_watchdogs() -> None:
    from runrail.scheduler.service import check_watchdogs
    with SessionLocal() as db:
        check_watchdogs(db)


def run_count(workflow_id: int) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(WorkflowRun)
                         .where(WorkflowRun.workflow_id == workflow_id)) or 0


def workflow_state(workflow_id: int) -> Workflow:
    with SessionLocal() as db:
        return db.get(Workflow, workflow_id)


def breach_marker(run_id: int):
    with SessionLocal() as db:
        return db.get(WorkflowRun, run_id).sla_breached_at


def test_snooze_mutes_alerts_but_still_auto_pauses(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "noisy", auto_pause_failures=2)
    make_shell_task(client, workflow["id"], "job", "exit 1")

    snoozed = client.post(f"/api/workflows/{workflow['id']}/snooze", json={
        "until": (now() + timedelta(hours=2)).isoformat()}).json()
    assert snoozed["snoozed"] is True and snoozed["snooze_pauses_runs"] is False

    for _ in range(2):
        client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
        execute_queued_run(client)
    # Muted, but the protection still acted: snooze silences talking, not doing.
    assert sent == []
    assert client.get(f"/api/workflows/{workflow['id']}").json()["enabled"] is False


def test_pause_runs_stops_enqueueing_and_plain_snooze_does_not(client):
    from runrail.scheduler.service import enqueue_scheduled

    muted = make_workflow(client, "muted", schedule_cron="*/5 * * * *")
    paused = make_workflow(client, "paused", schedule_cron="*/5 * * * *")
    expired = make_workflow(client, "expired", schedule_cron="*/5 * * * *")
    configure(muted["id"], snooze_until=now() + timedelta(hours=2))
    configure(paused["id"], snooze_until=now() + timedelta(hours=2), snooze_pauses_runs=True)
    configure(expired["id"], snooze_until=now() - timedelta(seconds=1), snooze_pauses_runs=True)
    for workflow in (muted, paused, expired):
        enqueue_scheduled(workflow["id"])

    assert run_count(muted["id"]) == 1     # mute-only: the nightly load still runs
    assert run_count(paused["id"]) == 0    # the escalation, opt-in and visible
    assert run_count(expired["id"]) == 1   # nothing re-enables it; only the clock moves


def test_snooze_endpoints_set_convert_and_clear(client):
    workflow = make_workflow(client, "muteable")

    target = now() + timedelta(hours=2)
    body = client.post(f"/api/workflows/{workflow['id']}/snooze", json={
        "until": target.astimezone(timezone(timedelta(hours=4))).isoformat(),
        "pause_runs": True}).json()
    assert body["snoozed"] is True and body["snooze_pauses_runs"] is True
    # The offset has to survive the write: SQLite keeps wall-clock components and
    # drops the tz, so an unconverted "+04:00" instant would land four hours out.
    stored = datetime.fromisoformat(body["snooze_until"])
    assert abs(stored - target) < timedelta(seconds=2)

    # Duration shorthand, for callers that cannot compute a viewer-local instant.
    shorthand = client.post(f"/api/workflows/{workflow['id']}/snooze?minutes=90").json()
    assert shorthand["snoozed"] is True and shorthand["snooze_pauses_runs"] is False

    base = f"/api/workflows/{workflow['id']}/snooze"
    assert client.post(base, json={"until": (now() - timedelta(minutes=1)).isoformat()}
                       ).status_code == 422
    assert client.post(base, json={"until": (now() + timedelta(days=31)).isoformat()}
                       ).status_code == 422
    assert client.post(base).status_code == 422                      # neither form given
    assert client.post(f"{base}?minutes=0").status_code == 422
    assert client.post("/api/workflows/9999/snooze?minutes=5").status_code == 404

    cleared = client.delete(base).json()
    assert cleared["snoozed"] is False and cleared["snooze_until"] is None
    assert cleared["snooze_pauses_runs"] is False


def test_missed_run_alerts_once_and_recovers(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "nightly", schedule_cron="*/5 * * * *",
                             missed_run_grace_minutes=10)
    add_run(workflow["id"], RunStatus.success, age_minutes=90)
    configure(workflow["id"], aged_minutes=120)
    edited_at = workflow_state(workflow["id"]).updated_at

    run_watchdogs()
    assert [n["event"] for n in sent] == ["run_missed"]
    assert "not run since" in sent[0]["text"]
    run_watchdogs()
    assert len(sent) == 1                  # the marker holds for the whole outage
    # The watchdog's own write must not read as an operator edit: updated_at
    # anchors the check, and bumping it would move the next expected fire.
    assert workflow_state(workflow["id"]).updated_at == edited_at

    add_run(workflow["id"], RunStatus.success)
    run_watchdogs()
    assert [n["event"] for n in sent] == ["run_missed", "run_missed_recovered"]
    assert workflow_state(workflow["id"]).missed_notified_at is None


def test_a_backed_up_or_gated_workflow_is_not_missing(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    for name, status in (("slow", RunStatus.running), ("gated", RunStatus.waiting_approval)):
        workflow = make_workflow(client, name, schedule_cron="*/5 * * * *",
                                 missed_run_grace_minutes=10)
        add_run(workflow["id"], status, age_minutes=90)
        configure(workflow["id"], aged_minutes=120)

    # Coalescing drops a fire while an iteration is in flight, and an approval
    # gate parks a run for as long as a human takes. Neither is silence.
    run_watchdogs()
    assert sent == []


def test_grace_window_is_respected(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "minutely", schedule_cron="* * * * *",
                             missed_run_grace_minutes=10)
    run_id = add_run(workflow["id"], RunStatus.success, age_minutes=5)
    configure(workflow["id"], aged_minutes=60)

    run_watchdogs()
    assert sent == []                      # late, but inside the tolerance

    age_run(run_id, minutes=20)
    run_watchdogs()
    assert [n["event"] for n in sent] == ["run_missed"]


def test_opt_outs_and_an_unparseable_crontab_stay_silent(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    off = make_workflow(client, "off", schedule_cron="* * * * *")
    bad = make_workflow(client, "bad", missed_run_grace_minutes=5)
    for workflow in (off, bad):
        add_run(workflow["id"], RunStatus.success, age_minutes=600)
        configure(workflow["id"], aged_minutes=600)
    # The API rejects this now, so only a YAML import or a hand-edited row can
    # still hold it — written straight to the column, exactly as it would arrive.
    configure(bad["id"], schedule_cron="not a crontab", aged_minutes=600)

    run_watchdogs()
    assert sent == []                      # NULL grace is off; a bad cron is skipped, not raised


def test_a_paused_workflow_still_alerts_and_says_so(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "forgotten", schedule_cron="* * * * *",
                             missed_run_grace_minutes=5)
    add_run(workflow["id"], RunStatus.success, age_minutes=120)
    configure(workflow["id"], enabled=False, aged_minutes=120)

    # "Someone paused it and forgot" is the likeliest dead pipeline, so `enabled`
    # is not a precondition — the message names it instead.
    run_watchdogs()
    assert [n["event"] for n in sent] == ["run_missed"]
    assert "paused" in sent[0]["text"]


def test_expected_fire_uses_the_workflow_timezone(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "dubai", schedule_cron="0 9 * * *",
                             schedule_timezone="Asia/Dubai", missed_run_grace_minutes=30)
    add_run(workflow["id"], RunStatus.success, age_minutes=3 * 24 * 60)
    configure(workflow["id"], aged_minutes=3 * 24 * 60)

    run_watchdogs()
    assert [n["event"] for n in sent] == ["run_missed"]
    # 09:00 in Dubai (UTC+4, no DST) is 05:00 UTC: the alert must name the instant
    # the scheduler would actually have fired, not the wall-clock crontab field —
    # and the message says "UTC", so the hour printed there has to agree.
    expected = datetime.fromisoformat(sent[0]["expected_at"]).astimezone(timezone.utc)
    assert expected.hour == 5
    assert "05:00 UTC" in sent[0]["text"]


def test_a_snoozed_workflow_fires_neither_watchdog(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    silent = make_workflow(client, "silent", schedule_cron="* * * * *",
                           missed_run_grace_minutes=5)
    add_run(silent["id"], RunStatus.success, age_minutes=120)
    configure(silent["id"], snooze_until=now() + timedelta(hours=2), aged_minutes=120)

    late = make_workflow(client, "late", sla_minutes=30)
    breaching = add_run(late["id"], RunStatus.running, age_minutes=120)
    configure(late["id"], snooze_until=now() + timedelta(hours=2))

    run_watchdogs()
    assert sent == []
    # No markers either: a mute must leave nothing behind that fires the moment
    # it lifts — neither a recovery message nor a late-finish one.
    assert workflow_state(silent["id"]).missed_notified_at is None
    assert breach_marker(breaching) is None


def test_snooze_expiry_does_not_immediately_alert(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "overnight", schedule_cron="0 3 * * *",
                             missed_run_grace_minutes=30)
    add_run(workflow["id"], RunStatus.success, age_minutes=12 * 60)
    # The mute ended a minute ago. Without snooze_until in the anchor, a workflow
    # muted overnight would alert the instant the snooze expired.
    configure(workflow["id"], snooze_until=now() - timedelta(minutes=1), aged_minutes=12 * 60)

    run_watchdogs()
    assert sent == []


def test_sla_breach_alerts_once_while_the_run_is_still_going(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "deadline", sla_minutes=60)
    run_id = add_run(workflow["id"], RunStatus.running, age_minutes=100,
                     trigger=TriggerType.manual)

    run_watchdogs()
    assert [n["event"] for n in sent] == ["sla_breached"]
    assert sent[0]["run_id"] == run_id and sent[0]["status"] == "running"
    assert breach_marker(run_id) is not None

    # The marker is on the run, so the query filters it out — no per-workflow
    # state to reset and no repeat however long it overruns.
    run_watchdogs()
    assert len(sent) == 1


def test_sla_catches_a_run_that_never_started_and_exempts_backfills(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    stuck = make_workflow(client, "stuck", sla_minutes=30)
    never_started = add_run(stuck["id"], RunStatus.queued, age_minutes=100)
    fresh = add_run(stuck["id"], RunStatus.running, age_minutes=5)

    bulk = make_workflow(client, "bulk", sla_minutes=30)
    for _ in range(3):
        add_run(bulk["id"], RunStatus.queued, age_minutes=100, trigger=TriggerType.backfill)

    run_watchdogs()
    # created_at is the origin, so "the worker died and it never started" breaches;
    # a burst of backfill runs draining a 30-day range does not.
    assert [n["run_id"] for n in sent] == [never_started]
    assert breach_marker(fresh) is None


def test_a_run_parked_on_a_human_breaches_like_any_other(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "gated", sla_minutes=30)
    parked = add_run(workflow["id"], RunStatus.waiting_approval, age_minutes=480)

    # "Blocked on a human who has already been asked" used to exempt this run
    # entirely: eight hours past a thirty-minute deadline and not a word. The
    # deadline is the promise, and nobody kept it.
    run_watchdogs()
    assert [n["run_id"] for n in sent] == [parked]
    assert sent[0]["status"] == "waiting_approval"
    assert breach_marker(parked) is not None


def test_only_the_oldest_in_flight_run_is_blamed(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "gated-nightly", sla_minutes=30)
    parked = add_run(workflow["id"], RunStatus.waiting_approval, age_minutes=480)
    following = add_run(workflow["id"], RunStatus.queued, age_minutes=240)

    # The coalesced iteration is late because of the gate in front of it. One
    # alert, and it names the run that actually missed the deadline.
    run_watchdogs()
    assert [n["run_id"] for n in sent] == [parked]
    assert breach_marker(following) is None
    # And the marker on the parked run must not hand the alert down the queue on
    # the next tick — that is the same incident wearing the wrong run's number.
    run_watchdogs()
    assert len(sent) == 1
    assert breach_marker(following) is None


def test_a_breached_run_reports_how_late_it_finished(client, monkeypatch):
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "late-finisher", sla_minutes=1)
    make_shell_task(client, workflow["id"], "job", "printf ok")
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()
    age_run(run["id"], minutes=100)

    run_watchdogs()
    assert [n["event"] for n in sent] == ["sla_breached"]

    # Success closes the loop; a breached run that then fails sends only run_failed.
    execute_queued_run(client)
    assert [n["event"] for n in sent] == ["sla_breached", "sla_finished_late"]


def test_the_watchdog_job_is_registered_and_survives_reconciliation(client):
    from runrail.scheduler.service import SchedulerService

    service = SchedulerService()
    try:
        service.start()
        assert "watchdog" in {job.id for job in service.scheduler.get_jobs()}
        # sync() removes every job it does not recognise every 30 seconds.
        service.sync()
        assert "watchdog" in {job.id for job in service.scheduler.get_jobs()}
    finally:
        service.shutdown()
