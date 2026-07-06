"""Concurrent execution: different workflows run in parallel, the same workflow
is serialized by max_concurrent_runs, and the scheduler coalesces iterations."""
import threading
import time


def make_workflow_with_task(client, name, command="echo ok", max_concurrent_runs=1):
    workflow = client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": max_concurrent_runs,
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "main", "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    return workflow


def queue_run(client, workflow_id):
    return client.post(f"/api/workflows/{workflow_id}/run", json={"parameters": {}}).json()


def test_different_workflows_are_claimable_together(client):
    fast = make_workflow_with_task(client, "fast")
    slow = make_workflow_with_task(client, "slow")
    queue_run(client, slow["id"])
    queue_run(client, fast["id"])

    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run

    with SessionLocal() as db:
        first = claim_next_run(db)
        second = claim_next_run(db)
    assert first is not None and second is not None
    assert {first.workflow_id, second.workflow_id} == {fast["id"], slow["id"]}


def test_same_workflow_runs_are_serialized_until_capacity_frees(client):
    workflow = make_workflow_with_task(client, "serialized")
    first_run = queue_run(client, workflow["id"])
    second_run = queue_run(client, workflow["id"])

    from runrail.db import SessionLocal
    from runrail.models import RunStatus, WorkflowRun, now
    from runrail.worker.queue import claim_next_run

    with SessionLocal() as db:
        claimed = claim_next_run(db)
        assert claimed is not None and claimed.id == first_run["id"]
        # The workflow is at max_concurrent_runs=1, so the second run must wait.
        assert claim_next_run(db) is None
        # Once the first run finishes, the queued iteration becomes claimable.
        run = db.get(WorkflowRun, claimed.id)
        run.status = RunStatus.success
        run.finished_at = now()
        db.commit()
        next_claimed = claim_next_run(db)
        assert next_claimed is not None and next_claimed.id == second_run["id"]


def test_max_concurrent_runs_above_one_allows_parallel_iterations(client):
    workflow = make_workflow_with_task(client, "wide", max_concurrent_runs=2)
    queue_run(client, workflow["id"])
    queue_run(client, workflow["id"])

    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run

    with SessionLocal() as db:
        assert claim_next_run(db) is not None
        assert claim_next_run(db) is not None
        assert claim_next_run(db) is None


def test_worker_pool_executes_workflows_concurrently(client):
    a = make_workflow_with_task(client, "concurrent-a", command="sleep 1")
    b = make_workflow_with_task(client, "concurrent-b", command="sleep 1")
    run_a = queue_run(client, a["id"])
    run_b = queue_run(client, b["id"])

    from runrail.db import SessionLocal
    from runrail.models import WorkflowRun
    from runrail.worker.service import WorkerService

    service = WorkerService(concurrency=4)
    thread = threading.Thread(target=service.run, kwargs={"install_signals": False}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                runs = [db.get(WorkflowRun, run_a["id"]), db.get(WorkflowRun, run_b["id"])]
            if all(r is not None and r.status.value == "success" for r in runs):
                break
            time.sleep(0.2)
        else:
            raise AssertionError(f"Runs did not finish: {[r.status.value for r in runs]}")
    finally:
        service.stop()
        thread.join(timeout=10)

    # Both one-second runs must have overlapped — proof they did not execute serially.
    first, second = runs
    assert first.started_at < second.finished_at
    assert second.started_at < first.finished_at


def test_environment_rebuild_waits_until_no_running_run_uses_it(client):
    environment = client.post("/api/environments", json={
        "name": "shared-env", "env_type": "python", "create_venv": True,
    }).json()

    from runrail.db import SessionLocal
    from runrail.environments import claim_next_environment, provision_managed
    from runrail.models import RunStatus, WorkflowRun, now
    from runrail.worker.queue import claim_next_run

    with SessionLocal() as db:
        provision_managed(db, claim_next_environment(db))

    workflow = client.post("/api/workflows", json={
        "name": "env-user", "enabled": True, "max_concurrent_runs": 1,
        "default_environment_id": environment["id"],
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "main", "task_type": "shell", "command": "echo ok",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    with SessionLocal() as db:
        claimed = claim_next_run(db)
        assert claimed is not None and claimed.id == run["id"]

    # Queue a rebuild while the run is executing: it must not be claimable yet.
    rebuild = client.post(f"/api/environments/{environment['id']}/rebuild", json={"packages": []})
    assert rebuild.status_code == 200
    with SessionLocal() as db:
        assert claim_next_environment(db) is None
        finished = db.get(WorkflowRun, run["id"])
        finished.status = RunStatus.success
        finished.finished_at = now()
        db.commit()
        # With the run finished, the deferred build becomes claimable.
        assert claim_next_environment(db) is not None


def test_run_is_requeued_while_its_environment_is_building(client):
    environment = client.post("/api/environments", json={
        "name": "building-env", "env_type": "python", "create_venv": True,
    }).json()

    from runrail.db import SessionLocal
    from runrail.environments import claim_next_environment, provision_managed
    from runrail.models import Environment, EnvironmentStatus, WorkflowRun
    from runrail.worker.service import _building_environment

    with SessionLocal() as db:
        provision_managed(db, claim_next_environment(db))

    workflow = client.post("/api/workflows", json={
        "name": "requeue-flow", "enabled": True, "max_concurrent_runs": 1,
        "default_environment_id": environment["id"],
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "main", "task_type": "shell", "command": "echo ok",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    with SessionLocal() as db:
        db.get(Environment, environment["id"]).status = EnvironmentStatus.building
        db.commit()
        blocked = _building_environment(db, db.get(WorkflowRun, run["id"]))
        assert blocked is not None and blocked.id == environment["id"]
        db.get(Environment, environment["id"]).status = EnvironmentStatus.ready
        db.commit()
        assert _building_environment(db, db.get(WorkflowRun, run["id"])) is None


def test_invalid_package_specs_are_rejected_at_creation(client):
    rejected = client.post("/api/environments", json={
        "name": "bad-specs", "env_type": "python", "create_venv": True,
        "packages": ["--find-links=/tmp/evil"],
    })
    assert rejected.status_code == 400
    assert "Invalid package requirement" in rejected.json()["detail"]


def test_scheduler_queues_next_iteration_while_one_is_running(client):
    workflow = make_workflow_with_task(client, "frequent")

    from runrail.db import SessionLocal
    from runrail.models import RunStatus, WorkflowRun
    from runrail.scheduler.service import enqueue_scheduled
    from runrail.worker.queue import claim_next_run

    def queued_count():
        with SessionLocal() as db:
            return db.query(WorkflowRun).filter(
                WorkflowRun.workflow_id == workflow["id"],
                WorkflowRun.status == RunStatus.queued,
            ).count()

    def pretend_tick_was_a_minute_ago(run_id):
        # run_key embeds the schedule minute; rewrite it so the next tick's key is free.
        with SessionLocal() as db:
            run = db.get(WorkflowRun, run_id)
            run.run_key = f"schedule:{workflow['id']}:previous-{run_id}"
            db.commit()

    # First tick queues a run; the worker claims it (now running).
    enqueue_scheduled(workflow["id"])
    with SessionLocal() as db:
        claimed = claim_next_run(db)
        assert claimed is not None
    pretend_tick_was_a_minute_ago(claimed.id)

    # While it is still running the next iteration is queued — not dropped.
    enqueue_scheduled(workflow["id"])
    assert queued_count() == 1

    # But iterations coalesce: another tick does not stack a second queued run.
    with SessionLocal() as db:
        waiting = db.query(WorkflowRun).filter(
            WorkflowRun.workflow_id == workflow["id"],
            WorkflowRun.status == RunStatus.queued,
        ).one()
    pretend_tick_was_a_minute_ago(waiting.id)
    enqueue_scheduled(workflow["id"])
    assert queued_count() == 1
