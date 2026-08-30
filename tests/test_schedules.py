"""Standard-crontab semantics, from the form down to the trigger.

APScheduler's day_of_week field counts 0=Mon..6=Sun; crontab, the schedule
builder and every operator count 0=Sun..6=Sat. Every weekday expectation below
is therefore read off the trigger the scheduler itself builds rather than
written down, so a translation that drifts — or an APScheduler upgrade that
renumbers — fails here instead of on a Tuesday morning.
"""

from datetime import datetime, timedelta, timezone

from runrail.crontab import cron_trigger
from runrail.db import SessionLocal
from runrail.models import Workflow

#: A Sunday. Only the weekday matters; every walk starts here.
ANCHOR = datetime(2026, 8, 30, tzinfo=timezone.utc)


def cron_dow(moment: datetime) -> int:
    """The standard-cron day number of an instant: 0=Sun..6=Sat."""
    return moment.isoweekday() % 7


def walk(expr: str, count: int, tz: str = "UTC") -> list[datetime]:
    """`count` successive fires of the repo's own trigger, from ANCHOR."""
    trigger = cron_trigger(expr, tz)
    fires: list[datetime] = []
    previous, cursor = None, ANCHOR
    for _ in range(count):
        fire = trigger.get_next_fire_time(previous, cursor)
        assert fire is not None, expr
        fires.append(fire)
        previous, cursor = fire, fire + timedelta(seconds=1)
    return fires


def make(client, name, **extra):
    return client.post("/api/workflows", json={"name": name, **extra})


def test_each_weekday_digit_fires_on_the_day_standard_cron_names_it():
    # The whole of defect 1 in three lines: shipped in v0.4.0, every one of these
    # landed a day late, so "every Monday" ran on Tuesday for every user.
    for digit in range(7):
        assert [cron_dow(fire) for fire in walk(f"0 9 * * {digit}", 3)] == [digit] * 3, digit
    assert walk("0 9 * * 1", 1)[0] == datetime(2026, 8, 31, 9, tzinfo=timezone.utc)


def test_seven_is_sunday_rather_than_a_workflow_that_never_runs():
    # 7 is valid crontab for Sunday and has no home in APScheduler's 0-6 field.
    # Raw, it raised, sync() skipped the workflow, and the schedule silently
    # ceased to exist.
    assert walk("0 9 * * 7", 3) == walk("0 9 * * 0", 3)
    assert {cron_dow(fire) for fire in walk("0 9 * * 7", 3)} == {0}


def test_lists_ranges_and_steps_keep_their_standard_cron_meaning():
    # Fourteen fires is two full weeks, so every expected day appears and no
    # unexpected one hides.
    for field, expected in (
        ("1-5", {1, 2, 3, 4, 5}),      # the weekday range every ops team writes
        ("5-7", {5, 6, 0}),            # a range running off the end of the week
        ("0-2", {0, 1, 2}),            # ... and one starting on Sunday
        ("*/2", {0, 2, 4, 6}),         # a step counts from Sunday, not from Monday
        ("1-5/2", {1, 3, 5}),
        ("6,0", {6, 0}),
        ("0,7", {0}),                  # both spellings of the same day
        ("mon,wed", {1, 3}),           # names were never ambiguous and still work
    ):
        assert {cron_dow(fire) for fire in walk(f"0 9 * * {field}", 14)} == expected, field


def test_the_scheduler_the_watchdog_and_the_gap_report_share_one_dialect(client):
    """Three call sites built their own trigger and only one of them can be
    fixed at a time; they are pinned to each other here so they cannot drift."""
    from runrail.schedule_gaps import _trigger
    from runrail.scheduler.service import SchedulerService, _expected_fire

    cron, zone = "30 8 * * 1", "Europe/Berlin"
    assert make(client, "monday-close", schedule_cron=cron, schedule_timezone=zone,
                enabled=True).status_code in (200, 201)

    service = SchedulerService()
    try:
        service.scheduler.start(paused=True)
        service.sync()
        job = next(job for job in service.scheduler.get_jobs()
                   if job.id.startswith("workflow-"))
        registered = job.trigger.get_next_fire_time(None, ANCHOR)
    finally:
        service.shutdown()

    workflow = Workflow(schedule_cron=cron, schedule_timezone=zone)
    assert _expected_fire(workflow, ANCHOR) == registered
    assert _trigger(workflow).get_next_fire_time(None, ANCHOR) == registered
    # A Monday. The same crontab used to be enqueued for Tuesday while the run
    # list, the gap report and the missed-run alert all said Monday.
    assert cron_dow(registered) == 1


#: Crontabs APScheduler cannot run, with the field an operator has to go and fix.
REJECTED = (
    ("60 9 * * *", "minute"),        # the one that previewed as "daily at 10:00"
    ("0 24 * * *", "hour"),
    ("99 99 * * *", "minute"),
    ("*/61 * * * *", "minute"),      # a step the two engines used to disagree about
    ("0 */25 * * *", "hour"),
    ("0 9 32 * *", "day-of-month"),
    ("0 9 * 13 *", "month"),
    ("0 9 * * 8", "day-of-week"),
    ("@daily", None),                # no field to name: not five fields at all
    ("every tuesday-ish", None),
)

#: Everything the builder can emit, plus the crontab spellings it cannot.
ACCEPTED = ("* * * * *", "*/15 * * * *", "0 * * * *", "0 9 * * *", "0 6 15 * *",
            "30 9 * * 1,3,5", "0 9 * * 7", "0 9 * * 1-5", "0 9 * * mon")


def test_the_api_refuses_a_crontab_the_scheduler_could_only_skip(client):
    for expr, field in REJECTED:
        response = make(client, f"bad {expr}", schedule_cron=expr)
        assert response.status_code == 422, expr
        assert "cron" in response.text.lower(), expr
        if field:
            assert field in response.text, expr
    # Nothing half-saved: a rejected schedule leaves no workflow behind.
    assert client.get("/api/workflows").json() == []


def test_everything_the_api_accepts_gets_a_scheduler_job(client):
    """The invariant the validation exists for: accepted and runnable are the
    same set, because they ask the same parser."""
    from runrail.scheduler.service import SchedulerService

    for expr in ACCEPTED:
        assert make(client, f"ok {expr}", schedule_cron=expr,
                    enabled=True).status_code in (200, 201), expr

    service = SchedulerService()
    try:
        service.scheduler.start(paused=True)
        service.sync()
        jobs = [job for job in service.scheduler.get_jobs() if job.id.startswith("workflow-")]
    finally:
        service.shutdown()
    assert len(jobs) == len(ACCEPTED)


def test_an_edit_cannot_smuggle_a_broken_crontab_past_the_form(client):
    workflow = make(client, "nightly", schedule_cron="0 9 * * *").json()
    payload = {"name": "nightly", "schedule_cron": "0 24 * * *"}
    response = client.put(f"/api/workflows/{workflow['id']}", json=payload)
    assert response.status_code == 422 and "hour" in response.text
    # And the schedule that was working is still the one that is stored.
    assert client.get(f"/api/workflows/{workflow['id']}").json()["schedule_cron"] == "0 9 * * *"


def test_a_blank_schedule_is_manual_runs_and_padding_is_trimmed(client):
    for index, blank in enumerate((None, "", "   ")):
        body = make(client, f"manual-{index}", schedule_cron=blank).json()
        assert body["schedule_cron"] is None, repr(blank)
    assert make(client, "padded", schedule_cron="  0 9 * * *  ").json()["schedule_cron"] \
        == "0 9 * * *"


def test_a_row_that_predates_the_check_is_still_readable(client):
    """YAML import and hand edits write the column directly. A workflow list that
    500s because one row is unrunnable would be worse than the unrunnable row —
    schedule-gaps is where it is reported, and it says so out loud."""
    workflow = make(client, "imported", schedule_cron="0 9 * * *").json()
    with SessionLocal() as db:
        db.get(Workflow, workflow["id"]).schedule_cron = "0 24 * * *"
        db.commit()

    listed = client.get("/api/workflows")
    assert listed.status_code == 200
    assert [flow["schedule_cron"] for flow in listed.json()] == ["0 24 * * *"]
    gaps = client.get(f"/api/workflows/{workflow['id']}/schedule-gaps").json()
    assert gaps["complete"] is False and gaps["stopped_by"] == "invalid_cron"
