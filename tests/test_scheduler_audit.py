"""The scheduler and its watchdogs, audited: each test is a false alarm, a
silent miss, or a fire that read as missed when it was not."""

from datetime import UTC, datetime, timedelta

import pytest
from test_watchdogs import capture_webhooks, configure, make_workflow, run_watchdogs

from runrail.crontab import cron_trigger, validate_cron
from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, WorkflowRun, now
from runrail.schedule_gaps import _match, _paused_spans, _walk


def test_a_backlog_that_just_drained_is_not_a_missed_schedule(client, monkeypatch):
    """An hourly run overran by two hours; the next iteration waited behind it
    (the fires in between were coalesced, by design) and ran just now. The
    watchdog anchored on that iteration's creation, two hours back, and said
    the schedule had gone quiet — a minute after a run had finished."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "hourly", schedule_cron="0 * * * *")
    configure(workflow["id"], aged_minutes=24 * 60, missed_run_grace_minutes=30)
    current = now()
    hour = current.replace(minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        db.add(WorkflowRun(workflow_id=workflow["id"], status=RunStatus.success,
                           trigger_type=TriggerType.schedule, created_at=hour - timedelta(hours=3),
                           started_at=hour - timedelta(hours=3),
                           finished_at=current - timedelta(seconds=25)))
        db.add(WorkflowRun(workflow_id=workflow["id"], status=RunStatus.success,
                           trigger_type=TriggerType.schedule, created_at=hour - timedelta(hours=2),
                           started_at=current - timedelta(seconds=20),
                           finished_at=current - timedelta(seconds=5)))
        db.commit()
    run_watchdogs()
    assert [s["event"] for s in sent if s["event"] == "run_missed"] == []


def test_deadlines_are_checked_for_every_run_already_going(client, monkeypatch):
    """Only the oldest in-flight run of a workflow was looked at. With two
    allowed at once, the second could run hours past its deadline unreported
    while the first stayed marked."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "parallel", sla_minutes=30, max_concurrent_runs=2)
    with SessionLocal() as db:
        for minutes in (120, 90):
            db.add(WorkflowRun(workflow_id=workflow["id"], status=RunStatus.running,
                               trigger_type=TriggerType.schedule,
                               created_at=now() - timedelta(minutes=minutes),
                               started_at=now() - timedelta(minutes=minutes)))
        # A follower that has not started is late because of the others: quiet.
        db.add(WorkflowRun(workflow_id=workflow["id"], status=RunStatus.queued,
                           trigger_type=TriggerType.schedule, created_at=now() - timedelta(minutes=60)))
        db.commit()
    run_watchdogs()
    assert [s["event"] for s in sent] == ["sla_breached", "sla_breached"]


@pytest.mark.parametrize("zone, start, end", [
    ("America/New_York", datetime(2026, 11, 1, 3, tzinfo=UTC), datetime(2026, 11, 1, 9, tzinfo=UTC)),
    ("Europe/London", datetime(2026, 10, 25, 0, tzinfo=UTC), datetime(2026, 10, 25, 4, tzinfo=UTC)),
    ("Europe/London", datetime(2026, 3, 29, 0, tzinfo=UTC), datetime(2026, 3, 29, 4, tzinfo=UTC)),
])
@pytest.mark.parametrize("expr", ["0 * * * *", "*/15 * * * *", "0 1-5 * * *"])
def test_a_clock_change_day_has_no_phantom_misses(zone, start, end, expr):
    """Arithmetic on zone-aware fires is wall-clock arithmetic, so around a
    change the matching window landed an hour off and real runs read as too
    old; and pinned hours across the spring gap produced two fires at one
    instant, one of which the scheduler never made."""
    fires, _ = _walk(cron_trigger(expr, zone), start, end, 200)
    assert all(fire.tzinfo is not None and fire.utcoffset() == timedelta(0) for fire in fires)
    assert len(fires) == len(set(fires)), "the same instant twice"
    runs = sorted(fire + timedelta(seconds=0.2) for fire in fires)
    assert all(_match(fires, runs)), "a fire with a run on it read as missed"


def test_a_cron_restricting_both_day_fields_is_refused():
    """Standard cron runs `0 9 1 * 1` on the 1st AND on every Monday; the
    scheduler ran it only when the 1st was a Monday — once or twice a year —
    and said nothing. New expressions say which they mean; existing ones run
    as before."""
    with pytest.raises(ValueError, match="day-of-month or day-of-week"):
        validate_cron("0 9 1 * 1")
    assert validate_cron("0 9 1 * *") == "0 9 1 * *"
    assert validate_cron("0 9 * * 1") == "0 9 * * 1"
    assert validate_cron("0 9 */2 * *") == "0 9 */2 * *"
    cron_trigger("0 9 1 * 1")  # a stored schedule still loads


def test_an_auto_pause_is_dated_from_the_pause_not_the_last_edit(client):
    from runrail.models import Workflow
    edited = now() - timedelta(days=10)
    paused = now() - timedelta(days=2)
    workflow = Workflow(name="w", enabled=False, updated_at=edited, auto_paused_at=paused)
    spans = _paused_spans(workflow, now() - timedelta(days=30), now())
    assert spans and spans[0]["reason"] == "disabled" and spans[0]["since"] == paused
