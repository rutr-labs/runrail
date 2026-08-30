"""Guards for the polled endpoints at a size the toy fixtures never reach.

Not a benchmark — wall-clock is the thing a loaded CI box gets wrong, and a
timing assertion would either be too loose to catch anything or too tight to
trust. What this pins is the *shape* of the work, which is deterministic:

  * how many statements a request runs, and whether that number moves when the
    response gets longer (an N+1 makes it move);
  * whether the database can answer the hot filters from an index, or has to
    read and sort every run ever recorded to return one page (the plan says so).

Both are exactly what changed between "a few hundred runs" and a year of
history, and neither costs more than the few thousand rows seeded below.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert, select

from runrail.models import (
    Environment,
    Project,
    RunStatus,
    Task,
    TaskRun,
    TaskRunStatus,
    TaskType,
    TriggerType,
    Workflow,
    WorkflowRun,
)

#: Enough history that a lost index shows up as a sort or a scan, and few enough
#: rows that the whole module seeds in well under a second.
RUNS = 3000
TASK_RUNS_ON_ONE_RUN = 40


@contextmanager
def counted(engine):
    """Statements issued inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def plan(connection, statement) -> str:
    compiled = statement.compile(connection, compile_kwargs={"literal_binds": True})
    return "\n".join(row[3] for row in
                     connection.exec_driver_sql("EXPLAIN QUERY PLAN " + str(compiled)))


@pytest.fixture()
def history(client):
    """One busy workflow and one quiet one, with a run history in between.

    Inserted straight through the core rather than over the API: this module is
    about how the *reads* behave at size, and 3000 POSTs would dominate its
    runtime for nothing.
    """
    import runrail.db as database

    with database.SessionLocal() as db:
        project = Project(name="scale", root_path="/tmp")
        environment = Environment(name="scale-env")
        db.add_all([project, environment])
        db.commit()
        db.add_all([Workflow(name="busy", schedule_cron="*/5 * * * *"), Workflow(name="quiet")])
        db.commit()
        busy, quiet = db.scalars(select(Workflow).order_by(Workflow.id)).all()
        # Distinct tasks, not one repeated: a run whose task runs all point at
        # the same task is answered from the session's identity map, and would
        # hide the per-row load this module exists to catch.
        db.execute(insert(Task), [{
            "workflow_id": workflow_id, "name": f"step_{index}", "task_type": TaskType.shell,
            "command": "true", "project_id": None, "environment_id": None,
            "script_path": None, "notebook_path": None, "sql_path": None, "cwd": None,
            "depends_on_json": [], "parameters_json": None, "retries": 0,
            "retry_delay_seconds": 60, "timeout_seconds": None, "requires_approval": False,
            "approval_prompt": None,
        } for workflow_id in (busy.id, quiet.id) for index in range(TASK_RUNS_ON_ONE_RUN)])
        db.commit()
        busy_tasks = list(db.scalars(select(Task.id).where(Task.workflow_id == busy.id)
                                     .order_by(Task.id)))

        start = datetime.now(timezone.utc) - timedelta(days=30)
        db.execute(insert(WorkflowRun), [{
            "workflow_id": busy.id if index % 20 else quiet.id,
            "status": RunStatus.failed if index % 9 == 0 else RunStatus.success,
            "trigger_type": TriggerType.schedule, "run_key": None, "parameters_json": {},
            "started_at": start + timedelta(minutes=index),
            "finished_at": start + timedelta(minutes=index, seconds=30),
            "duration_seconds": 30.0, "created_at": start + timedelta(minutes=index),
            "resume_count": 0, "sla_breached_at": None,
        } for index in range(RUNS)])
        db.commit()
        # Two runs of the same workflow with very different task-list lengths:
        # the pair is what makes a run-detail regression measurable, and both
        # have to be non-empty or the eager load's own query is what differs.
        slim, fat = db.scalars(select(WorkflowRun.id)
                               .where(WorkflowRun.workflow_id == busy.id)
                               .order_by(WorkflowRun.id.desc()).limit(2)).all()
        db.execute(insert(TaskRun), [{
            "workflow_run_id": run_id, "task_id": busy_tasks[index],
            "status": TaskRunStatus.success,
            "attempt": 1, "started_at": start, "finished_at": start + timedelta(seconds=1),
            "duration_seconds": 1.0, "exit_code": 0, "stdout_log_path": None,
            "stderr_log_path": None, "error_message": None, "rendered_command": "true",
            "created_at": start, "resume_index": 0, "approval_note": None, "approved_at": None,
        } for run_id, count in ((slim, 2), (fat, TASK_RUNS_ON_ONE_RUN))
            for index in range(count)])
        db.commit()
        return {"busy": busy.id, "quiet": quiet.id, "slim_run": slim, "fat_run": fat}


def test_run_list_costs_the_same_however_long_the_page(client, history):
    """An N+1 hides here: 10 rows and 400 rows must cost the same statements."""
    import runrail.db as database

    counts = []
    for limit in (10, 400):
        with counted(database.engine) as statements:
            response = client.get(f"/api/runs?workflow_id={history['busy']}&limit={limit}")
        assert response.status_code == 200
        assert len(response.json()) == limit
        counts.append(len(statements))
    assert counts[0] == counts[1] == 1, counts


def test_run_detail_costs_the_same_however_many_task_runs(client, history):
    """The eager load on WorkflowRun.task_runs is what keeps this flat; losing
    it makes the run page issue one query per task run."""
    import runrail.db as database

    def statements_for(run_id: int) -> int:
        with counted(database.engine) as statements:
            assert client.get(f"/api/runs/{run_id}").status_code == 200
        return len(statements)

    slim = statements_for(history["slim_run"])
    fat = statements_for(history["fat_run"])
    assert len(client.get(f"/api/runs/{history['fat_run']}").json()["task_runs"]) == \
        TASK_RUNS_ON_ONE_RUN
    assert slim == fat, (slim, fat)


def test_polled_endpoints_stay_within_a_handful_of_statements(client, history):
    """Every one of these is fetched on a timer while a dashboard sits open, so
    a per-workflow or per-run query added to any of them compounds forever."""
    import runrail.db as database

    budget = {
        "/api/activity": 6,
        "/api/stats/summary": 10,
        "/api/stats/daily?days=7": 2,
        f"/api/workflows/{history['busy']}/task-durations": 4,
        f"/api/workflows/{history['busy']}/schedule-gaps?days=2": 3,
        "/api/runs/notes/summary": 3,
        "/api/approvals": 2,
    }
    for url, allowed in budget.items():
        with counted(database.engine) as statements:
            assert client.get(url).status_code == 200
        assert len(statements) <= allowed, f"{url} ran {len(statements)} statements:\n" + \
            "\n".join(statements)


def test_hot_filters_are_answered_from_an_index(client, history):
    """The three shapes that read history rather than a page of it.

    Each has to be satisfied by a composite index; a plan that sorts, or one
    that scans workflow_runs, means the request now reads every run ever
    recorded to answer a question about the newest few.
    """
    import runrail.db as database

    if database.engine.dialect.name != "sqlite":
        pytest.skip("EXPLAIN QUERY PLAN and its wording are SQLite's")
    cases = {
        # The run list, filtered and ordered — the wallboard's and the workflow
        # page's query.
        "ix_workflow_runs_workflow_id_created_at": select(WorkflowRun).where(
            WorkflowRun.workflow_id == history["busy"]).order_by(
            WorkflowRun.created_at.desc()).limit(100),
        # /api/stats/summary counts this shape three times per poll.
        "ix_workflow_runs_status_created_at": select(WorkflowRun.id).where(
            WorkflowRun.status == RunStatus.success,
            WorkflowRun.created_at >= datetime.now(timezone.utc) - timedelta(days=1)),
        # The activity feed's SLA source: mostly-NULL column, read every poll.
        "ix_workflow_runs_sla_breached_at": select(WorkflowRun.id).where(
            WorkflowRun.sla_breached_at.is_not(None)).order_by(
            WorkflowRun.sla_breached_at.desc()).limit(200),
    }
    with database.engine.connect() as connection:
        for index, statement in cases.items():
            detail = plan(connection, statement)
            assert index in detail, f"expected {index} in:\n{detail}"
            assert "SCAN workflow_runs" not in detail, detail
            assert "TEMP B-TREE" not in detail, detail
