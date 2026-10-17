"""Backfills beside the live schedule.

A backfill is bulk work queued by hand. It must drain without taking the
schedule's turn, be bounded, and carry its own date into every template."""

from datetime import date, datetime, timezone

import pytest
from test_interactions import make_shell_task, make_workflow, start_run

from runrail.api.crud import MAX_BACKFILL_DAYS, create_backfill
from runrail.db import SessionLocal
from runrail.models import Project, Task, TaskRun, TriggerType, Workflow, WorkflowRun
from runrail.worker.queue import claim_next_run
from runrail.worker.service import _context


def test_the_schedule_takes_its_turn_ahead_of_a_queued_backfill(client):
    """Ordered by age alone, a month of backfill rows queued before the 09:00
    iteration held it for the hours they took to drain — and every fire
    meanwhile found that iteration still queued and was dropped."""
    workflow = make_workflow(client, "hourly", max_concurrent_runs=1)
    make_shell_task(client, workflow["id"], "t", "exit 0")
    queued = client.post(f"/api/workflows/{workflow['id']}/backfill", json={
        "from": "2026-09-01", "to": "2026-09-10", "parameters": {}}).json()
    assert len(queued) == 10
    live = start_run(client, workflow["id"])  # the schedule, arriving last
    with SessionLocal() as db:
        first = claim_next_run(db)
        assert first is not None and first.id == live
        second = claim_next_run(db)  # max_concurrent_runs=1: nothing else yet
        assert second is None


def test_a_backfill_range_is_bounded_and_overflow_free(client):
    workflow = make_workflow(client, "daily")
    make_shell_task(client, workflow["id"], "t", "exit 0")
    url = f"/api/workflows/{workflow['id']}/backfill"
    too_long = client.post(url, json={"from": "2020-01-01", "to": "2030-01-01", "parameters": {}})
    assert too_long.status_code == 400 and str(MAX_BACKFILL_DAYS) in too_long.text
    backwards = client.post(url, json={"from": "2026-02-01", "to": "2026-01-01", "parameters": {}})
    assert backwards.status_code == 400
    # The last days the calendar has: iterating past the end used to overflow.
    with SessionLocal() as db:
        runs = create_backfill(db, db.get(Workflow, workflow["id"]), date(9999, 12, 29), date.max)
    assert [r.parameters_json["ds"] for r in runs] == ["9999-12-29", "9999-12-30", "9999-12-31"]
    # Dates already backfilled are skipped, not duplicated.
    with SessionLocal() as db:
        assert create_backfill(db, db.get(Workflow, workflow["id"]), date(9999, 12, 30), date.max) == []


def test_a_backfill_date_wins_over_a_task_default_and_sets_ts(client):
    """A task that happened to define `ds` made every run of a 30-day backfill
    process the same day, and `ts` named the moment the range was queued."""
    workflow = make_workflow(client, "dated")
    make_shell_task(client, workflow["id"], "t", "exit 0", parameters_json={"ds": "1999-01-01", "region": "ca"})
    client.post(f"/api/workflows/{workflow['id']}/backfill", json={
        "from": "2026-03-05", "to": "2026-03-05", "parameters": {"region": "eu"}})
    with SessionLocal() as db:
        run = db.query(WorkflowRun).filter(WorkflowRun.trigger_type == TriggerType.backfill).one()
        task = db.query(Task).one()
        task_run = TaskRun(workflow_run_id=run.id, task_id=task.id, attempt=1, resume_index=0, status="running")
        db.add(task_run); db.flush()
        context = _context(run, task_run, task, db.get(Project, task.project_id) if task.project_id else None)
    assert context["ds"] == "2026-03-05"
    assert context["ts"].startswith("2026-03-05T00:00:00") and context["ts_nodash"] == "20260305T000000"
    # One-off run values override task defaults, as the "Run with…" form promises.
    assert context["parameters"]["region"] == "eu"


def test_a_cli_backfill_with_a_bad_range_is_a_clean_error(client, monkeypatch):
    from typer.testing import CliRunner

    from runrail.cli import app
    workflow = make_workflow(client, "cli")
    make_shell_task(client, workflow["id"], "t", "exit 0")
    result = CliRunner().invoke(app, ["backfill", "cli", "--from", "2026-02-01", "--to", "2026-01-01"])
    assert result.exit_code != 0
    assert "on or after" in result.output and "Traceback" not in result.output


def test_a_data_directory_import_is_refused_when_the_database_lives_elsewhere(tmp_path, monkeypatch):
    """The copy landed in a file the app never opens, and the summary still
    said "Imported" with a workflow count."""
    from typer.testing import CliRunner

    from runrail.cli import app
    from runrail.config import get_settings
    monkeypatch.setenv("RUNRAIL_DB_URL", f"sqlite:///{tmp_path / 'elsewhere.db'}")
    get_settings.cache_clear()
    source = tmp_path / "old"; source.mkdir(); (source / "runrail.db").write_bytes(b"")
    result = CliRunner().invoke(app, ["import", str(source)])
    get_settings.cache_clear()
    assert result.exit_code != 0 and "RUNRAIL_DB_URL is set" in result.output


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    monkeypatch.setattr("runrail.worker.service.now", lambda: datetime.now(timezone.utc))
