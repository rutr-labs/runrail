"""Missed scheduled runs as history: what the schedule owed, what ran, and the
spans where nothing was owed at all.

Every fire time in these tests is read back out of the module rather than
hand-computed — a test that reimplemented cron would agree with a bug. What the
fires themselves are pinned against is the scheduler's own _expected_fire.
"""

from datetime import datetime, timedelta, timezone

from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun, now

HOURLY = "0 * * * *"


def make_workflow(client, name, **extra):
    response = client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1, **extra})
    assert response.status_code in (200, 201), response.text
    return response.json()


def configure(workflow_id: int, *, age_days: float = 30, **values):
    """Write straight onto the row.

    created_at bounds the window and updated_at anchors the paused spans, so
    both are always set explicitly — letting onupdate bump updated_at would
    silently move the very edge under test.
    """
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        for key, value in values.items():
            setattr(workflow, key, value)
        workflow.created_at = values.get("created_at", now() - timedelta(days=age_days))
        workflow.updated_at = values.get("updated_at", workflow.created_at)
        db.commit()


def add_run(workflow_id, at: datetime, *, trigger=TriggerType.schedule,
            status=RunStatus.success, ran=True) -> int:
    """A run created at `at`. `ran=False` leaves it queued forever — the shape a
    dead worker leaves behind."""
    with SessionLocal() as db:
        run = WorkflowRun(
            workflow_id=workflow_id, status=status, trigger_type=trigger, created_at=at,
            started_at=at + timedelta(seconds=1) if ran else None,
            finished_at=at + timedelta(seconds=2) if ran else None)
        db.add(run); db.commit()
        return run.id


def gaps(client, workflow_id, **params):
    response = client.get(f"/api/workflows/{workflow_id}/schedule-gaps", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def missed_at(body) -> list[datetime]:
    """The missed fires, ascending (the API returns them newest first)."""
    return [datetime.fromisoformat(entry["expected_at"]) for entry in body["missed"]][::-1]


def expected_fires(client, workflow_id, **params) -> list[datetime]:
    """Every fire the window expects, ascending — read off a workflow that has
    never run, where by construction every fire is a gap."""
    return missed_at(gaps(client, workflow_id, **params))


def test_an_outage_reports_exactly_the_fires_it_swallowed(client):
    workflow = make_workflow(client, "hourly", schedule_cron=HOURLY)
    configure(workflow["id"])
    fires = expected_fires(client, workflow["id"], days=2)
    assert len(fires) >= 24

    outage = fires[5:9]
    for fire in fires:
        if fire not in outage:
            add_run(workflow["id"], fire)

    body = gaps(client, workflow["id"], days=2)
    # The lid was closed for four hours: exactly those four, and the runs that
    # did happen are counted as runs rather than left as suspects.
    assert missed_at(body) == outage
    assert body["totals"] == {"expected": len(fires), "ran": len(fires) - 4,
                              "missed": 4, "blocked": 0, "paused": 0}
    assert body["complete"] is True and body["stopped_by"] is None


def test_a_late_run_answers_its_fire_and_a_much_later_one_does_not(client):
    workflow = make_workflow(client, "late", schedule_cron=HOURLY)
    configure(workflow["id"])
    fires = expected_fires(client, workflow["id"], days=1)

    # misfire_grace_time lets a fire land up to 55s late, so a run stamped 50s
    # after its fire is that fire, not a gap beside it.
    for fire in fires[:-1]:
        add_run(workflow["id"], fire + timedelta(seconds=50))
    add_run(workflow["id"], fires[-1] + timedelta(minutes=5))

    body = gaps(client, workflow["id"], days=1)
    # Five minutes is past every tolerance: that fire never happened, and the
    # stray run answers for no other fire either.
    assert missed_at(body) == [fires[-1]]
    assert body["totals"]["ran"] == len(fires) - 1


def test_a_manual_run_does_not_answer_a_scheduled_fire(client):
    workflow = make_workflow(client, "hand-cranked", schedule_cron=HOURLY)
    configure(workflow["id"])
    fires = expected_fires(client, workflow["id"], days=1)
    for fire in fires:
        add_run(workflow["id"], fire + timedelta(seconds=30), trigger=TriggerType.manual)

    # Identical timing, different trigger: someone pressing the button says
    # nothing about whether the schedule fired.
    body = gaps(client, workflow["id"], days=1)
    assert missed_at(body) == fires
    assert body["totals"]["ran"] == 0


def test_fires_from_before_the_workflow_existed_are_not_owed(client):
    old = make_workflow(client, "veteran", schedule_cron=HOURLY)
    young = make_workflow(client, "newborn", schedule_cron=HOURLY)
    configure(old["id"], age_days=30)
    born = now() - timedelta(hours=5)
    configure(young["id"], created_at=born, updated_at=born)

    every = expected_fires(client, old["id"], days=14, limit=400)
    body = gaps(client, young["id"], days=14, limit=400)
    # The same schedule, truncated at birth — a workflow created this morning
    # has not been failing for a fortnight.
    assert missed_at(body) == [fire for fire in every if fire >= born]
    assert datetime.fromisoformat(body["window"]["requested_since"]) < born
    assert abs(datetime.fromisoformat(body["window"]["since"]) - born) < timedelta(seconds=2)


def test_a_pause_runs_snooze_reads_as_paused_not_as_a_thousand_gaps(client):
    workflow = make_workflow(client, "muted", schedule_cron=HOURLY)
    muted_at = now() - timedelta(hours=6)
    configure(workflow["id"], created_at=now() - timedelta(days=30), updated_at=muted_at,
              snooze_until=now() + timedelta(hours=2), snooze_pauses_runs=True)

    body = gaps(client, workflow["id"], days=1)
    span = body["paused_spans"][0]
    assert span["reason"] == "snoozed"
    # The mute is deliberate, so the fires inside it were never owed; the day
    # before it still shows the truth.
    assert all(fire < muted_at for fire in missed_at(body))
    assert body["totals"]["paused"] >= 5 and body["totals"]["missed"] >= 15
    assert body["totals"]["paused"] + body["totals"]["missed"] == body["totals"]["expected"]


def test_a_disabled_workflow_reports_the_pause_rather_than_a_wall_of_red(client):
    workflow = make_workflow(client, "parked", schedule_cron=HOURLY)
    paused_at = now() - timedelta(hours=8)
    configure(workflow["id"], created_at=now() - timedelta(days=30), updated_at=paused_at,
              enabled=False)

    body = gaps(client, workflow["id"], days=1)
    assert [span["reason"] for span in body["paused_spans"]] == ["disabled"]
    assert all(fire < paused_at for fire in missed_at(body))
    assert body["totals"]["paused"] >= 7


def test_a_run_that_actually_happened_beats_the_reconstructed_pause(client):
    workflow = make_workflow(client, "evidence", schedule_cron=HOURLY)
    born = now() - timedelta(days=30)
    configure(workflow["id"], created_at=born, updated_at=born)
    fires = expected_fires(client, workflow["id"], days=1)  # read while still live

    paused_at = now() - timedelta(hours=6)
    configure(workflow["id"], created_at=born, updated_at=paused_at, enabled=False)
    inside = [fire for fire in fires if fire > paused_at]
    add_run(workflow["id"], inside[0])
    body = gaps(client, workflow["id"], days=1)
    # updated_at is a proxy for when the operator paused it, not a record. A run
    # that exists is proof the workflow was live, and proof wins.
    assert body["totals"]["ran"] == 1
    assert body["totals"]["paused"] == len(inside) - 1


def test_fires_the_scheduler_deliberately_coalesced_are_not_misses(client):
    workflow = make_workflow(client, "backed-up", schedule_cron=HOURLY)
    configure(workflow["id"])
    stuck_at = now() - timedelta(hours=4)
    add_run(workflow["id"], stuck_at, status=RunStatus.queued, ran=False)

    body = gaps(client, workflow["id"], days=1)
    # enqueue_scheduled returns early while a run sits queued, so those fires
    # were dropped by design. Calling them missed would blame the schedule for
    # the worker being dead — that is the SLA watchdog's job to report.
    assert all(fire < stuck_at for fire in missed_at(body))
    assert body["totals"]["blocked"] >= 3
    assert (body["totals"]["ran"] + body["totals"]["missed"] + body["totals"]["blocked"]
            == body["totals"]["expected"])


def test_the_cap_keeps_the_newest_fires_and_says_it_truncated(client):
    workflow = make_workflow(client, "minutely", schedule_cron="* * * * *")
    configure(workflow["id"])

    body = gaps(client, workflow["id"], days=1, max_fires=10)
    # A day of "* * * * *" is 1440 fires. Truncation is inevitable; claiming a
    # clean day would be the bug, and the newest end is the useful one.
    assert body["complete"] is False and body["stopped_by"] == "max_fires"
    assert body["totals"]["expected"] == 10
    window = {key: datetime.fromisoformat(value) for key, value in body["window"].items()}
    assert window["until"] - window["since"] < timedelta(minutes=11)
    assert window["requested_since"] < window["since"]
    assert window["until"] - missed_at(body)[-1] < timedelta(minutes=2)


def test_the_missed_list_is_capped_while_the_counts_stay_whole(client):
    workflow = make_workflow(client, "chatty", schedule_cron="*/5 * * * *")
    configure(workflow["id"])

    body = gaps(client, workflow["id"], days=1, limit=20)
    assert body["missed_shown"] == 20 and body["totals"]["missed"] > 100
    # Capping the rows must never cap the heatmap: a day of 5-minute fires is
    # 288 cells and every one of them is counted.
    assert sum(day["missed"] for day in body["daily"]) == body["totals"]["missed"]
    assert body["complete"] is True


def test_the_run_query_has_a_valve_of_its_own(client, monkeypatch):
    """The fire cap bounds the window, not the rows inside it: a workflow that
    used to run every minute and now runs hourly still has a dense history."""
    import runrail.schedule_gaps as schedule_gaps

    workflow = make_workflow(client, "dense-history", schedule_cron=HOURLY)
    configure(workflow["id"])
    for fire in expected_fires(client, workflow["id"], days=1)[:3]:
        add_run(workflow["id"], fire)

    monkeypatch.setattr(schedule_gaps, "MAX_RUN_ROWS", 2)
    body = gaps(client, workflow["id"], days=1)
    assert body["complete"] is False and body["stopped_by"] == "run_rows"


def test_the_daily_buckets_add_up_and_are_dated_in_utc(client):
    workflow = make_workflow(client, "heatmap", schedule_cron=HOURLY,
                             schedule_timezone="Asia/Dubai")
    configure(workflow["id"])
    fires = expected_fires(client, workflow["id"], days=3)
    add_run(workflow["id"], fires[-1])

    body = gaps(client, workflow["id"], days=3)
    assert [day["date"] for day in body["daily"]] == sorted(day["date"] for day in body["daily"])
    assert sum(day["expected"] for day in body["daily"]) == len(fires)
    # UTC dates, like /stats/daily: a missed cell has to land in the same column
    # of the same heatmap as the runs beside it.
    for day in body["daily"]:
        assert day["expected"] == day["ran"] + day["missed"] + day["blocked"] + day["paused"]
        counted = [fire for fire in fires
                   if fire.astimezone(timezone.utc).date().isoformat() == day["date"]]
        assert len(counted) == day["expected"]


def test_fires_follow_the_workflow_timezone_across_a_dst_boundary(client):
    from runrail.schedule_gaps import _fires, _trigger

    workflow = Workflow(schedule_cron="0 2 * * *", schedule_timezone="America/New_York")
    fires, truncated = _fires(_trigger(workflow),
                              datetime(2026, 3, 5, tzinfo=timezone.utc),
                              datetime(2026, 3, 11, tzinfo=timezone.utc), 100)

    hours = {fire.astimezone(timezone.utc).date().isoformat(): fire.astimezone(timezone.utc).hour
             for fire in fires}
    # 02:00 in New York is 07:00 UTC under EST and 06:00 UTC under EDT. A cron
    # evaluated in UTC would put every one of these at 02:00 and report six
    # phantom gaps a day either side of the switch.
    assert hours["2026-03-06"] == 7 and hours["2026-03-07"] == 7
    assert hours["2026-03-09"] == 6 and hours["2026-03-10"] == 6
    assert truncated is False


def test_the_fires_are_the_watchdogs_own_expectation(client):
    """The one thing that must never drift: this module and the scheduler have
    to agree about when a fire was due, or a DST boundary makes one of them
    alert about a run the other knows never happened."""
    from runrail.schedule_gaps import _fires, _trigger
    from runrail.scheduler.service import _expected_fire

    for cron, zone in (("0 2 * * *", "America/New_York"), ("*/17 * * * *", "Asia/Dubai")):
        workflow = Workflow(schedule_cron=cron, schedule_timezone=zone)
        since = datetime(2026, 3, 5, tzinfo=timezone.utc)
        fires, _ = _fires(_trigger(workflow), since,
                          datetime(2026, 3, 9, tzinfo=timezone.utc), 500)
        assert fires and _expected_fire(workflow, since) == fires[0]
        for previous, following in zip(fires, fires[1:], strict=False):
            assert _expected_fire(workflow, previous + timedelta(seconds=1)) == following


def test_an_unscheduled_or_unparseable_workflow_answers_honestly(client):
    unscheduled = make_workflow(client, "on-demand")
    broken = make_workflow(client, "typo")
    configure(unscheduled["id"])
    # The API validates crontabs, so only a hand-edited or imported row gets
    # here — the same silent skip sync() makes, said out loud.
    configure(broken["id"], schedule_cron="every tuesday-ish")

    quiet = gaps(client, unscheduled["id"])
    assert quiet["totals"]["expected"] == 0 and quiet["complete"] is True

    bad = gaps(client, broken["id"])
    assert bad["totals"]["expected"] == 0
    assert bad["complete"] is False and bad["stopped_by"] == "invalid_cron"


def test_unknown_workflows_and_out_of_range_bounds(client):
    workflow = make_workflow(client, "bounded", schedule_cron=HOURLY)
    assert client.get("/api/workflows/9999/schedule-gaps").status_code == 404
    for params in ({"days": 0}, {"days": 400}, {"max_fires": 9}, {"max_fires": 10001},
                   {"limit": 0}):
        response = client.get(f"/api/workflows/{workflow['id']}/schedule-gaps", params=params)
        assert response.status_code == 422, params
