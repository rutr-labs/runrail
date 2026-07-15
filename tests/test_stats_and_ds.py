"""SQL-aggregated dashboard stats, and the scheduled-run `ds` behavior."""

from datetime import datetime, timedelta, timezone


def make_wf(client, name):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()


def test_stats_summary_aggregates_in_sql(client):
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, TriggerType, WorkflowRun, now

    wf = make_wf(client, "agg")
    with SessionLocal() as db:
        for status, dur in [(RunStatus.success, 4.0), (RunStatus.success, 6.0), (RunStatus.failed, 2.0)]:
            db.add(WorkflowRun(workflow_id=wf["id"], status=status, trigger_type=TriggerType.manual,
                               duration_seconds=dur, finished_at=now()))
        old = WorkflowRun(workflow_id=wf["id"], status=RunStatus.success,
                          trigger_type=TriggerType.manual, duration_seconds=9.0)
        db.add(old)
        db.add(WorkflowRun(workflow_id=wf["id"], status=RunStatus.running, trigger_type=TriggerType.manual))
        db.add(WorkflowRun(workflow_id=wf["id"], status=RunStatus.queued, trigger_type=TriggerType.manual))
        db.commit()
        # Push the fourth success outside the 7-day window.
        db.query(WorkflowRun).filter(WorkflowRun.id == old.id).update(
            {"created_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)})
        db.commit()

    s = client.get("/api/stats/summary").json()
    assert s["running"] == 1 and s["queued"] == 1 and s["live"] == 2
    assert s["runs_24h"] == 5           # 3 completed + running + queued, all created now
    assert s["succeeded_24h"] == 2
    assert s["failed_24h"] == 1
    assert s["avg_duration_24h"] == 4.0  # (4 + 6 + 2) / 3; the 10-day-old run excluded
    assert s["done_7d"] == 3             # old success outside the window
    assert s["success_7d"] == 2
    assert s["success_rate_7d"] == 67    # round(2 / 3 * 100)


def test_scheduled_run_does_not_persist_a_ds_parameter(client):
    from runrail.db import SessionLocal
    from runrail.models import TriggerType, WorkflowRun
    from runrail.scheduler.service import enqueue_scheduled

    wf = make_wf(client, "sched")
    enqueue_scheduled(wf["id"])
    with SessionLocal() as db:
        run = db.query(WorkflowRun).filter(WorkflowRun.workflow_id == wf["id"]).one()
    assert run.trigger_type == TriggerType.schedule
    assert not (run.parameters_json or {}).get("ds")  # no spurious ds chip on the run


def test_ds_still_renders_when_no_parameter_is_passed(client):
    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run

    wf = make_wf(client, "ds-render")
    client.post(f"/api/workflows/{wf['id']}/tasks", json={
        "name": "emit", "task_type": "shell", "command": "printf '{{ ds }}'",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    run = client.post(f"/api/workflows/{wf['id']}/run", json={"parameters": {}}).json()
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))

    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["status"] == "success"
    stdout = client.get(f"/api/task-runs/{detail['task_runs'][0]['id']}/stdout").text
    # The worker still defaults `ds` to the run's date for templating.
    assert stdout == datetime.now(timezone.utc).strftime("%Y-%m-%d")
