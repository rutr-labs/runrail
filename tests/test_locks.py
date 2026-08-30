"""Resource locks: mutual exclusion between workflows on a named resource.

The motivating case is a monthly maintenance job that owns one database while it
runs — everything else on that database queues instead of racing it.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from runrail.db import Base
from runrail.models import LockMode, RunStatus, TriggerType, Workflow, WorkflowRun, now
from runrail.worker.queue import claim_next_run

RELEASED = (RunStatus.success, RunStatus.failed, RunStatus.cancelled)


def open_db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def add_workflow(db, name, resource=None, mode=LockMode.shared, limit=1) -> Workflow:
    workflow = Workflow(name=name, lock_resource=resource, lock_mode=mode,
                        max_concurrent_runs=limit)
    db.add(workflow); db.flush()
    return workflow


def enqueue(db, workflow, *, age_seconds: int = 0) -> WorkflowRun:
    """A queued run. `age_seconds` pins the claim order, which is by created_at."""
    run = WorkflowRun(workflow_id=workflow.id, trigger_type=TriggerType.manual,
                      created_at=now() - timedelta(seconds=age_seconds))
    db.add(run); db.commit()
    return run


def select_runs(workflow_id):
    return select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)


def claimed_workflow_ids(db, count: int) -> list[int | None]:
    return [None if run is None else run.workflow_id
            for run in (claim_next_run(db) for _ in range(count))]


def test_an_exclusive_run_blocks_a_shared_one():
    with open_db() as db:
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared)
        enqueue(db, heavy, age_seconds=60)
        enqueue(db, hourly)

        assert claimed_workflow_ids(db, 2) == [heavy.id, None]


def test_a_shared_run_blocks_an_exclusive_one():
    with open_db() as db:
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared)
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        enqueue(db, hourly)
        assert claim_next_run(db).workflow_id == hourly.id

        enqueue(db, heavy)
        assert claim_next_run(db) is None


def test_two_shared_runs_on_one_resource_overlap():
    with open_db() as db:
        reader = add_workflow(db, "report", "warehouse", LockMode.shared)
        other = add_workflow(db, "extract", "warehouse", LockMode.shared)
        enqueue(db, reader, age_seconds=60)
        enqueue(db, other)

        assert claimed_workflow_ids(db, 2) == [reader.id, other.id]


def test_an_exclusive_lock_serialises_a_workflow_against_itself():
    """The lock is held on the resource, not on the workflow: max_concurrent_runs
    stays a separate, narrower budget."""
    with open_db() as db:
        heavy = add_workflow(db, "monthly-maintenance", "warehouse",
                             LockMode.exclusive, limit=2)
        enqueue(db, heavy, age_seconds=60)
        enqueue(db, heavy)

        assert claimed_workflow_ids(db, 2) == [heavy.id, None]


def test_different_resource_names_never_interact():
    with open_db() as db:
        warehouse = add_workflow(db, "vacuum-warehouse", "warehouse", LockMode.exclusive)
        lake = add_workflow(db, "vacuum-lake", "lake", LockMode.exclusive)
        enqueue(db, warehouse, age_seconds=60)
        enqueue(db, lake)

        assert claimed_workflow_ids(db, 2) == [warehouse.id, lake.id]


def test_a_workflow_without_a_resource_is_unaffected():
    """NULL means no locking: an unlocked run neither waits nor makes others wait."""
    with open_db() as db:
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        free = add_workflow(db, "unrelated")
        enqueue(db, heavy, age_seconds=60)
        enqueue(db, free)
        # An exclusive hold on the warehouse stops nothing that never asked for it.
        assert claimed_workflow_ids(db, 2) == [heavy.id, free.id]

        db.scalars(select_runs(heavy.id)).one().status = RunStatus.success
        db.commit()
        # ...and the unlocked run in flight holds no resource of its own.
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared)
        enqueue(db, hourly)
        assert claim_next_run(db).workflow_id == hourly.id


def test_a_run_parked_on_an_approval_gate_still_holds_its_lock():
    """It has partial state and resumes into the same tasks, so releasing the
    resource would put a second writer on the same database."""
    with open_db() as db:
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared)
        enqueue(db, heavy, age_seconds=60)
        enqueue(db, hourly)

        parked = claim_next_run(db)
        parked.status = RunStatus.waiting_approval
        db.commit()
        assert claim_next_run(db) is None

        db.get(WorkflowRun, parked.id).status = RunStatus.success
        db.commit()
        assert claim_next_run(db).workflow_id == hourly.id


@pytest.mark.parametrize("terminal", RELEASED)
def test_the_lock_is_released_at_a_terminal_status(terminal):
    with open_db() as db:
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared)
        enqueue(db, heavy, age_seconds=60)
        enqueue(db, hourly)

        holder = claim_next_run(db)
        assert claim_next_run(db) is None
        holder.status = terminal
        db.commit()
        assert claim_next_run(db).workflow_id == hourly.id


def test_a_killed_worker_releases_the_lock_on_the_next_start():
    """Nothing in the lock code is crash-aware: the resource comes back only
    because startup recovery ends the run that was holding it."""
    from runrail.worker.service import recover_interrupted_runs

    with open_db() as db:
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared)
        enqueue(db, heavy, age_seconds=60)
        enqueue(db, hourly)

        # A claimed run is exactly the row a kill leaves behind: still 'running'.
        killed = claim_next_run(db)
        assert killed.workflow_id == heavy.id
        assert claim_next_run(db) is None      # the warehouse is locked, and stays locked

        assert recover_interrupted_runs(db) == 1
        assert db.get(WorkflowRun, killed.id).status == RunStatus.failed
        assert claim_next_run(db).workflow_id == hourly.id


def test_a_lock_held_at_an_approval_gate_survives_the_restart():
    """The complement, and deliberate: recovery leaves 'waiting_approval' alone
    because nobody has decided yet and the run resumes into the same tasks. So
    the resource stays held across a restart, and cancelling is what frees it."""
    from runrail.worker.service import recover_interrupted_runs

    with open_db() as db:
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared)
        enqueue(db, heavy, age_seconds=60)
        enqueue(db, hourly)

        parked = claim_next_run(db)
        parked.status = RunStatus.waiting_approval
        db.commit()

        assert recover_interrupted_runs(db) == 0
        assert db.get(WorkflowRun, parked.id).status == RunStatus.waiting_approval
        assert claim_next_run(db) is None

        parked.status = RunStatus.cancelled
        db.commit()
        assert claim_next_run(db).workflow_id == hourly.id


def test_a_queued_exclusive_run_bars_new_shared_runs():
    """Without the barrier a drip of hourly jobs starves the monthly maintenance."""
    with open_db() as db:
        hourly = add_workflow(db, "hourly-load", "warehouse", LockMode.shared, limit=2)
        heavy = add_workflow(db, "monthly-maintenance", "warehouse", LockMode.exclusive)
        enqueue(db, hourly, age_seconds=120)

        in_flight = claim_next_run(db)                  # a shared run is already going
        assert in_flight.workflow_id == hourly.id
        waiting = enqueue(db, heavy, age_seconds=60)    # maintenance queues behind it
        enqueue(db, hourly)                             # a NEW shared run arrives
        # Neither may start: the exclusive one waits on the holder, and the new
        # shared one waits on the exclusive one.
        assert claim_next_run(db) is None
        # A barrier, not a preemption — the run already holding it is untouched.
        assert db.get(WorkflowRun, in_flight.id).status == RunStatus.running

        in_flight.status = RunStatus.success
        db.commit()
        assert claim_next_run(db).id == waiting.id      # maintenance gets its turn
        assert claim_next_run(db) is None               # and now owns the resource alone


def test_lock_mode_is_inert_without_a_resource():
    from pydantic import ValidationError

    from runrail.schemas import WorkflowIn

    assert WorkflowIn(name="w", lock_mode="exclusive").lock_resource is None
    assert WorkflowIn(name="w", lock_mode="exclusive").lock_mode is LockMode.shared
    assert WorkflowIn(name="w", lock_resource="   ").lock_resource is None
    locked = WorkflowIn(name="w", lock_resource="  warehouse  ", lock_mode="exclusive")
    assert locked.lock_resource == "warehouse" and locked.lock_mode is LockMode.exclusive
    with pytest.raises(ValidationError):
        WorkflowIn(name="w", lock_resource="warehouse", lock_mode="wishful")


def test_lock_configuration_round_trips_through_export_and_apply(client):
    import yaml

    from runrail.db import SessionLocal
    from runrail.workflow_io import apply_workflows, export_workflows

    locked = client.post("/api/workflows", json={
        "name": "monthly-maintenance", "enabled": True, "max_concurrent_runs": 1,
        "lock_resource": "warehouse", "lock_mode": "exclusive"}).json()
    assert locked["lock_resource"] == "warehouse" and locked["lock_mode"] == "exclusive"
    client.post("/api/workflows", json={
        "name": "unlocked", "enabled": True, "max_concurrent_runs": 1})

    with SessionLocal() as db:
        data = export_workflows(db)
    exported = {item["name"]: item for item in data["workflows"]}
    assert exported["monthly-maintenance"]["lock_resource"] == "warehouse"
    assert exported["monthly-maintenance"]["lock_mode"] == "exclusive"
    # A mode without a resource is not configuration, and the file must stay YAML.
    assert not {"lock_resource", "lock_mode"} & exported["unlocked"].keys()
    assert "lock_mode: exclusive" in yaml.safe_dump(data, sort_keys=False)

    with SessionLocal() as db:
        assert apply_workflows(db, data) == {
            "created": [], "updated": ["monthly-maintenance", "unlocked"]}
    reapplied = {w["name"]: w for w in client.get("/api/workflows").json()}
    assert reapplied["monthly-maintenance"]["lock_mode"] == "exclusive"
    assert reapplied["unlocked"]["lock_resource"] is None
    assert reapplied["unlocked"]["lock_mode"] == "shared"

    # Dropping the keys from the file releases the lock.
    del exported["monthly-maintenance"]["lock_resource"]
    del exported["monthly-maintenance"]["lock_mode"]
    with SessionLocal() as db:
        apply_workflows(db, data)
    assert client.get(f"/api/workflows/{locked['id']}").json()["lock_resource"] is None
