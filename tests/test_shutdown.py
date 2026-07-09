"""Shutdown hygiene: phantom-run recovery and websocket log streams that
terminate cleanly instead of hanging graceful shutdown."""


def make_workflow_with_task(client, name):
    workflow = client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "job", "task_type": "shell", "command": "printf hello",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    return workflow


def test_recover_interrupted_runs_unblocks_the_workflow(client):
    from runrail.db import SessionLocal
    from runrail.models import (
        RunStatus,
        TaskRun,
        TaskRunStatus,
        TriggerType,
        WorkflowRun,
        now,
    )
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import recover_interrupted_runs

    workflow = make_workflow_with_task(client, "resilient")
    with SessionLocal() as db:
        # Simulate a force-killed worker: a run (and task run) stuck 'running'.
        phantom = WorkflowRun(workflow_id=workflow["id"], status=RunStatus.running,
                              trigger_type=TriggerType.manual, started_at=now())
        db.add(phantom); db.commit(); db.refresh(phantom)
        db.add(TaskRun(workflow_run_id=phantom.id, task_id=1, status=TaskRunStatus.running,
                       started_at=now()))
        db.commit()
        phantom_id = phantom.id

    # The phantom occupies the workflow's only concurrency slot: nothing claimable.
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    with SessionLocal() as db:
        assert claim_next_run(db) is None

    with SessionLocal() as db:
        assert recover_interrupted_runs(db) == 1
        assert recover_interrupted_runs(db) == 0  # idempotent

    detail = client.get(f"/api/runs/{phantom_id}").json()
    assert detail["status"] == "failed"
    assert detail["finished_at"] is not None
    assert all(t["error_message"] == "Interrupted by worker shutdown"
               for t in detail["task_runs"])

    with SessionLocal() as db:  # the queued run is claimable again
        claimed = claim_next_run(db)
        assert claimed is not None


def test_log_stream_sends_content_and_closes_for_finished_tasks(client):
    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run

    workflow = make_workflow_with_task(client, "streamer")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    run = client.get("/api/runs").json()[0]
    task_run_id = client.get(f"/api/runs/{run['id']}").json()["task_runs"][0]["id"]

    with client.websocket_connect(f"/api/ws/task-runs/{task_run_id}/logs?stream=stdout") as ws:
        assert ws.receive_text() == "hello"
        # The task is terminal, so the server must close promptly (no infinite poll).
        closed = ws.receive()
        assert closed["type"] == "websocket.close"
