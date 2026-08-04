"""Per-workflow schedule timezones: API validation, scheduler wiring, YAML round-trip."""

from datetime import datetime, timezone


def test_workflow_accepts_and_returns_schedule_timezone(client):
    created = client.post("/api/workflows", json={
        "name": "tz-flow", "schedule_cron": "0 9 * * *",
        "schedule_timezone": "Asia/Dubai"})
    assert created.status_code in (200, 201)
    assert created.json()["schedule_timezone"] == "Asia/Dubai"

    fetched = client.get(f"/api/workflows/{created.json()['id']}").json()
    assert fetched["schedule_timezone"] == "Asia/Dubai"


def test_unknown_timezone_is_rejected(client):
    response = client.post("/api/workflows", json={
        "name": "bad-tz", "schedule_cron": "0 9 * * *",
        "schedule_timezone": "Mars/Olympus_Mons"})
    assert response.status_code == 422
    assert "timezone" in response.text.lower()


def test_scheduler_evaluates_cron_in_workflow_timezone(client):
    client.post("/api/workflows", json={
        "name": "dubai-nine", "schedule_cron": "0 9 * * *",
        "schedule_timezone": "Asia/Dubai", "enabled": True})
    client.post("/api/workflows", json={
        "name": "utc-nine", "schedule_cron": "0 9 * * *", "enabled": True})

    from runrail.scheduler.service import SchedulerService
    service = SchedulerService()
    try:
        service.scheduler.start(paused=True)
        service.sync()
        jobs = {job.id: job for job in service.scheduler.get_jobs()}
        workflow_jobs = [jobs[j] for j in jobs if j.startswith("workflow-")]
        assert len(workflow_jobs) == 2
        by_tz = sorted(str(job.trigger.timezone) for job in workflow_jobs)
        assert by_tz == ["Asia/Dubai", "UTC"]

        # 09:00 in Dubai (UTC+4, no DST) is 05:00 UTC — the two identical
        # crontabs must fire four hours apart.
        after = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        fires = sorted(job.trigger.get_next_fire_time(None, after).astimezone(timezone.utc)
                       for job in workflow_jobs)
        assert fires[0] == datetime(2026, 8, 10, 5, 0, tzinfo=timezone.utc)
        assert fires[1] == datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    finally:
        service.shutdown()


def test_timezone_round_trips_through_export_and_apply(client):
    client.post("/api/workflows", json={
        "name": "tz-io", "schedule_cron": "30 8 * * 1",
        "schedule_timezone": "Europe/Berlin"})

    from runrail.db import SessionLocal
    from runrail.workflow_io import apply_workflows, export_workflows

    with SessionLocal() as db:
        exported = export_workflows(db, "tz-io")
    entry = exported["workflows"][0]
    assert entry["schedule_timezone"] == "Europe/Berlin"

    entry["schedule_timezone"] = "America/New_York"
    with SessionLocal() as db:
        apply_workflows(db, {"workflows": [entry]})
        db.commit()

    fetched = client.get("/api/workflows").json()
    flow = next(w for w in fetched if w["name"] == "tz-io")
    assert flow["schedule_timezone"] == "America/New_York"
