"""GET /runs?since&until: the runs the timeline draws."""


def test_runs_can_be_asked_for_by_the_stretch_of_time_they_overlap(client):
    """The timeline's query: every run that overlaps [since, until)."""
    from datetime import datetime, timedelta, timezone

    import runrail.db as database
    from runrail.models import RunStatus, TriggerType, WorkflowRun
    flow = client.post("/api/workflows", json={"name": "windowed", "enabled": True}).json()
    base = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    spans = {
        "before": (base - timedelta(hours=5), base - timedelta(hours=4)),     # ended before the window
        "into": (base - timedelta(hours=1), base + timedelta(minutes=30)),    # started before, ran into it
        "inside": (base + timedelta(hours=1), base + timedelta(hours=2)),
        "still": (base + timedelta(hours=2), None),                           # never finished
        "after": (base + timedelta(hours=7), base + timedelta(hours=8)),      # created after the window
    }
    ids = {}
    with database.SessionLocal() as db:
        for name, (start, end) in spans.items():
            run = WorkflowRun(workflow_id=flow["id"], trigger_type=TriggerType.manual,
                              status=RunStatus.running if end is None else RunStatus.success,
                              created_at=start, started_at=start, finished_at=end)
            db.add(run); db.commit(); ids[run.id] = name
    got = client.get("/api/runs", params={
        "workflow_id": flow["id"], "since": base.isoformat(), "until": (base + timedelta(hours=6)).isoformat()}).json()
    assert sorted(ids[r["id"]] for r in got) == ["inside", "into", "still"]
    # An instant with another offset means the same moment.
    shifted = client.get("/api/runs", params={
        "workflow_id": flow["id"], "since": "2026-11-20T07:00:00-05:00", "until": "2026-11-20T13:00:00-05:00"}).json()
    assert sorted(ids[r["id"]] for r in shifted) == ["inside", "into", "still"]
