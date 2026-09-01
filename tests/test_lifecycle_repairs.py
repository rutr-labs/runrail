"""Six lifecycle defects found by auditing the paths the feature tests skip.

Each of these was reproduced before it was fixed. They share a shape worth
naming: every one is a *second* action arriving while a run is mid-flight —
deleting the task a run waits on, deleting the environment a workflow points at,
queueing a backfill beside a schedule, retrying onto a workflow that has since
been emptied. The per-feature suites each set up their own world and tear it
down, so none of them could see a collision between two of those worlds.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text

from runrail.db import SessionLocal
from runrail.models import RunStatus, TaskRun, TaskRunStatus, TriggerType, WorkflowRun
from runrail.scheduler.service import enqueue_scheduled
from runrail.worker.service import (
    claim_runnable_run,
    execute_workflow_run,
    recover_interrupted_runs,
)
from tests.test_interactions import (
    backfill,
    make_shell_task,
    make_workflow,
    start_run,
)


def _park_on_gate(client, workflow_name="gated", **workflow_extra):
    """A run stopped on an approval gate, which is where several of these bite."""
    workflow = make_workflow(client, workflow_name, **workflow_extra)
    make_shell_task(client, workflow["id"], "stage", "printf s")
    gate = make_shell_task(client, workflow["id"], "publish", "printf p",
                           depends_on=["stage"], requires_approval=True)
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        run = claim_runnable_run(db)
        execute_workflow_run(db, run)
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "waiting_approval"
    return workflow, gate, run_id


def test_deleting_the_task_a_run_waits_on_releases_the_run(client):
    """TaskRun.task_id cascades, so deleting the task erased the gate row and
    left the run in waiting_approval with no exit — holding its concurrency slot
    and its resource lock, which then made every later scheduled fire coalesce
    into nothing. The workflow's schedule died silently."""
    workflow, gate, run_id = _park_on_gate(client, "gated-delete",
                                           lock_resource="warehouse", lock_mode="exclusive")
    neighbour = make_workflow(client, "neighbour", lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, neighbour["id"], "t", "printf n")
    neighbour_run = start_run(client, neighbour["id"])

    assert client.delete(f"/api/tasks/{gate['id']}").status_code == 204

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "cancelled", "the run must not be left waiting on a task that is gone"
    assert run["finished_at"], "a settled run needs a finish time"
    assert client.get("/api/approvals").json() == []

    # The lock has to come back with it, or every workflow sharing the resource
    # is stalled by a run nobody can decide.
    with SessionLocal() as db:
        claimed = claim_runnable_run(db)
        assert claimed is not None and claimed.id == neighbour_run


def test_deleting_a_workflows_environment_does_not_brick_it(client, tmp_path):
    """workflows.project_id and default_environment_id had no foreign key at
    all, so the model's ondelete=SET NULL never existed in the database. A
    deleted environment left the workflow pointing at a dead id: /run answered
    404 while the scheduler kept enqueueing it."""
    project = client.post("/api/projects", json={
        "name": "p", "root_path": str(tmp_path)}).json()
    environment = client.post("/api/environments", json={
        "name": "e", "executable": "/usr/bin/python3"}).json()
    workflow = make_workflow(client, "pointed", project_id=project["id"],
                             default_environment_id=environment["id"])
    make_shell_task(client, workflow["id"], "t", "printf t")

    assert client.delete(f"/api/environments/{environment['id']}").status_code == 204
    assert client.delete(f"/api/projects/{project['id']}").status_code == 204

    after = client.get(f"/api/workflows/{workflow['id']}").json()
    assert after["default_environment_id"] is None, "dangling environment id"
    assert after["project_id"] is None, "dangling project id — tasks would run in the wrong tree"
    assert client.post(f"/api/workflows/{workflow['id']}/run",
                       json={"parameters": {}}).status_code == 201

    with SessionLocal() as db:
        names = {row[3] for row in db.execute(text("PRAGMA foreign_key_list(workflows)"))}
    assert {"project_id", "default_environment_id"} <= names


def test_a_queued_backfill_does_not_suppress_the_schedule(client):
    """Coalescing counted every queued run, so a backfill range waiting to drain
    made each scheduled fire drop instead of defer — invisibly, since a queued
    run reads as in-flight to the missed-run watchdog."""
    workflow = make_workflow(client, "hourly", schedule_cron="0 * * * *")
    make_shell_task(client, workflow["id"], "t", "printf t")
    today = date(2026, 1, 1)
    queued_backfill = backfill(client, workflow["id"], today, today + timedelta(days=4))
    assert len(queued_backfill) == 5

    enqueue_scheduled(workflow["id"])   # opens its own session
    with SessionLocal() as db:
        scheduled = db.scalars(select_scheduled(workflow["id"])).all()
    assert scheduled, "the schedule was silently skipped while a backfill waited"


def select_scheduled(workflow_id):
    return select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id,
                                     WorkflowRun.trigger_type == TriggerType.schedule)


def test_retry_refuses_a_workflow_that_can_no_longer_run(client):
    """/retry skipped the check /run and /resume both apply, so it produced a
    run that failed with no task rows — and that phantom failure counted toward
    auto-pause, disabling the schedule off one click."""
    workflow = make_workflow(client, "emptied", auto_pause_failures=1)
    task = make_shell_task(client, workflow["id"], "only", "printf x")
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 204

    assert client.post(f"/api/workflows/{workflow['id']}/run",
                       json={"parameters": {}}).status_code == 409
    assert client.post(f"/api/runs/{run_id}/retry").status_code == 409
    assert client.get(f"/api/workflows/{workflow['id']}").json()["enabled"] is True


def test_a_resumed_segments_skips_are_filed_under_that_segment(client):
    """Only _run_task and open_gate stamped resume_index, so the rows the graph
    walker writes itself — skips, cancels, executor failures — all landed in
    segment 0. Segment filters then read an incomplete set."""
    workflow = make_workflow(client, "resumed")
    make_shell_task(client, workflow["id"], "a", "exit 1")
    make_shell_task(client, workflow["id"], "b", "printf b", depends_on=["a"])
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))
    assert client.post(f"/api/runs/{run_id}/resume").status_code == 200
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))

    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        rows = db.scalars(select_task_runs(run_id)).all()
        segments = {index: sorted(r.status.value for r in rows if r.resume_index == index)
                    for index in {r.resume_index for r in rows}}
    assert run.resume_count == 1
    # Each attempt ran both tasks: one failure and one skip, filed together.
    assert segments[1] == ["failed", "skipped"], segments


def select_task_runs(run_id):
    return select(TaskRun).where(TaskRun.workflow_run_id == run_id)


def test_recovery_settles_a_task_left_running_by_a_cancelled_run(client):
    """Cancel settles queued and awaiting_approval rows and leaves the live one
    to its worker. If the worker dies first, nothing repaired the row: the run
    page showed a task spinning forever on a run that had ended."""
    workflow = make_workflow(client, "cancel-crash")
    make_shell_task(client, workflow["id"], "t", "printf t")
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        run = claim_runnable_run(db)
        db.add(TaskRun(workflow_run_id=run.id, task_id=run.workflow.tasks[0].id,
                       status=TaskRunStatus.running, attempt=1, resume_index=0))
        db.commit()
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200

    with SessionLocal() as db:
        recover_interrupted_runs(db)
        left = db.scalars(select_task_runs(run_id)).all()
    assert not [r for r in left if r.status == TaskRunStatus.running], \
        "a terminal run must not keep a task row spinning"
    assert client.get(f"/api/runs/{run_id}").json()["status"] == RunStatus.cancelled.value


def test_runs_can_be_filtered_to_a_single_day(client):
    """The activity heatmap links each cell to its day's runs. The filter has to
    be server-side: the page holds the newest 500 runs, so a day older than
    those would otherwise filter down to nothing on a busy install."""
    workflow = make_workflow(client, "dated")
    make_shell_task(client, workflow["id"], "t", "printf t")
    start_run(client, workflow["id"])

    today = datetime.now(timezone.utc).date()
    same_day = client.get(f"/api/runs?day={today.isoformat()}&limit=500").json()
    assert same_day, "today's run is missing from today's filter"
    assert {run["created_at"][:10] for run in same_day} == {today.isoformat()}

    empty = client.get(f"/api/runs?day={(today - timedelta(days=400)).isoformat()}").json()
    assert empty == []
    # A cell can only ever send a real date; anything else is a client bug, and
    # a 422 says so rather than quietly listing everything.
    assert client.get("/api/runs?day=not-a-date").status_code == 422
