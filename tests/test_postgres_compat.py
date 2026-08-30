"""The SQL that SQLite alone cannot vouch for, run against the configured backend.

Every other suite builds its own in-memory SQLite engine or takes the default
one, so a construct that compiles everywhere and only *behaves* on SQLite passes
them all: sa.Enum is a bare VARCHAR there with no CHECK and no length
enforcement, DATETIME silently drops the offset, and one writer at a time hides
what a correlated subquery in an UPDATE really does. Point RUNRAIL_TEST_DB_URL at
a PostgreSQL server (CI's second backend job does) and these become the real
check; on SQLite they still pin the behaviour the two backends must share.
"""

from datetime import timedelta


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1, **extra,
    }).json()


def test_migration_added_enum_labels_round_trip(client):
    """The values ALTER TYPE ADD VALUE appended, and the lockmode type itself.

    On PostgreSQL every one of these is a label the migration had to add to a
    native type; a missed ALTER TYPE is an InvalidTextRepresentation on write,
    which no amount of SQLite coverage can surface.
    """
    from runrail.db import SessionLocal
    from runrail.models import (
        LockMode,
        RunStatus,
        Task,
        TaskRun,
        TaskRunStatus,
        TaskType,
        TriggerType,
        Workflow,
        WorkflowRun,
    )

    with SessionLocal() as db:
        workflow = Workflow(name="enum-labels", lock_resource="warehouse",
                            lock_mode=LockMode.exclusive)
        db.add(workflow); db.flush()
        task = Task(workflow_id=workflow.id, name="gated", task_type=TaskType.shell,
                    command="true", requires_approval=True)
        db.add(task); db.flush()
        run = WorkflowRun(workflow_id=workflow.id, trigger_type=TriggerType.manual,
                          status=RunStatus.waiting_approval)
        db.add(run); db.flush()
        db.add_all([TaskRun(workflow_run_id=run.id, task_id=task.id, attempt=0, status=status)
                    for status in (TaskRunStatus.awaiting_approval, TaskRunStatus.approved,
                                   TaskRunStatus.rejected)])
        db.commit()
        run_id, workflow_id = run.id, workflow.id

    with SessionLocal() as db:
        assert db.get(WorkflowRun, run_id).status == RunStatus.waiting_approval
        assert db.get(Workflow, workflow_id).lock_mode == LockMode.exclusive
        assert {row.status for row in db.get(WorkflowRun, run_id).task_runs} == {
            TaskRunStatus.awaiting_approval, TaskRunStatus.approved, TaskRunStatus.rejected}


def test_the_claim_statement_executes_on_this_backend(client):
    """claim_next_run's UPDATE, with its correlated count and resource EXISTS.

    test_queue and test_locks pin the semantics against an in-memory SQLite
    engine of their own; this runs the same statement wherever the suite points,
    so PostgreSQL gets a say on the shape as well as on the answer.
    """
    from runrail.db import SessionLocal
    from runrail.models import LockMode, RunStatus, TriggerType, Workflow, WorkflowRun, now
    from runrail.worker.queue import claim_next_run

    with SessionLocal() as db:
        holder = Workflow(name="maintenance", lock_resource="warehouse",
                          lock_mode=LockMode.exclusive)
        waiter = Workflow(name="hourly-load", lock_resource="warehouse",
                          lock_mode=LockMode.shared)
        db.add_all([holder, waiter]); db.flush()
        db.add(WorkflowRun(workflow_id=holder.id, trigger_type=TriggerType.manual,
                           created_at=now() - timedelta(seconds=10)))
        db.add(WorkflowRun(workflow_id=waiter.id, trigger_type=TriggerType.manual))
        db.commit()

        claimed = claim_next_run(db)
        assert claimed is not None and claimed.workflow_id == holder.id
        assert claimed.status == RunStatus.running
        # The exclusive holder bars the shared run until it lands terminal.
        assert claim_next_run(db) is None
        claimed.status = RunStatus.success; db.commit()
        released = claim_next_run(db)
        assert released is not None and released.workflow_id == waiter.id


def test_daily_stats_buckets_on_the_utc_day(client):
    """func.date() over a timestamp column, executed for real.

    PostgreSQL's date() truncates in the session timezone rather than in UTC, so
    the bucket a run lands in is a property of the connection, not of the query —
    db._make_engine is what pins it.
    """
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, TriggerType, WorkflowRun, now

    workflow = make_workflow(client, "heatmap")
    with SessionLocal() as db:
        db.add_all([
            WorkflowRun(workflow_id=workflow["id"], trigger_type=TriggerType.manual,
                        status=RunStatus.success),
            WorkflowRun(workflow_id=workflow["id"], trigger_type=TriggerType.manual,
                        status=RunStatus.failed),
        ])
        db.commit()

    days = client.get("/api/stats/daily").json()
    today = [day for day in days if day["date"] == now().date().isoformat()]
    assert today and today[0]["success"] == 1 and today[0]["failed"] == 1


def test_the_activity_feed_window_functions_execute(client):
    """LAG over a CASE projection and ROW_NUMBER, on a real result set.

    The feed reports a failure only on the transition, which is the LAG's whole
    job; an empty table would execute the window function without ever proving
    the projection types line up.
    """
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, TriggerType, WorkflowRun, now

    workflow = make_workflow(client, "transitions")
    with SessionLocal() as db:
        for status in (RunStatus.success, RunStatus.failed, RunStatus.failed):
            db.add(WorkflowRun(workflow_id=workflow["id"], trigger_type=TriggerType.manual,
                               status=status, finished_at=now()))
        db.commit()

    kinds = [event["kind"] for event in client.get("/api/activity").json()["events"]]
    assert kinds.count("run_failed") == 1  # the transition, not both red runs


def test_the_shutdown_summary_subtracts_timestamps_from_this_backend(client):
    """PostgreSQL returns aware datetimes where SQLite returns naive ones, and
    mixing the two in one subtraction raises TypeError."""
    from runrail.cli import _executing_summary
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, TriggerType, WorkflowRun, now

    workflow = make_workflow(client, "long-running")
    with SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow["id"], trigger_type=TriggerType.manual,
                          status=RunStatus.running, started_at=now())
        db.add(run); db.commit()
        run_id = run.id

    class _Worker:
        @staticmethod
        def executing_run_ids():
            return [run_id]

    assert f"run #{run_id} of 'long-running'" in _executing_summary(_Worker())[0]
