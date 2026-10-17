"""Retention cleanup deletes data, so each of these is a way it deleted the wrong
thing, or left the database and the disk disagreeing:

- Runs were selected by created_at. A resume keeps the original created_at on
  purpose, so a 40-day-old run resumed and finished today was deleted on the
  next pass with the output it had just produced — and a cancelled run whose
  worker was still stopping could be deleted out from under it.
- A workflow's newest successful run aged out like any other, so its pinned
  "latest report" link answered that it had never succeeded.
- Every file of every run was removed before one commit at the end: a failed
  commit left rows pointing at deleted files, a file that would not delete was
  orphaned for good, and on SQLite the write lock was held for all of the file
  I/O, locking the worker and the API out.
- A dry run reported 0 files.
"""

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from portable import emit
from test_interactions import make_shell_task, make_workflow, start_run

from runrail.config import Settings, get_settings
from runrail.db import SessionLocal
from runrail.maintenance import cleanup_runs
from runrail.models import Artifact, RunStatus, TaskRun, WorkflowRun, now
from runrail.worker.service import claim_runnable_run, execute_workflow_run


def _finished_run(client, workflow, age_days: float, status=RunStatus.success) -> int:
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        execute_workflow_run(db, claim_runnable_run(db))
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        stamp = now().replace(tzinfo=None) - timedelta(days=age_days)
        run.created_at, run.finished_at, run.status = stamp, stamp, status
        db.commit()
    return run_id


def _exists(run_id) -> bool:
    with SessionLocal() as db:
        return db.get(WorkflowRun, run_id) is not None


def _log_dir(run_id) -> Path:
    return get_settings().logs_dir.resolve() / f"run_{run_id}"


def test_a_run_that_finished_recently_is_kept_however_old_it_is(client):
    workflow = make_workflow(client, "resumed")
    make_shell_task(client, workflow["id"], "t", emit("ok"))
    _finished_run(client, workflow, age_days=1)  # the newest success, kept regardless
    resumed = _finished_run(client, workflow, age_days=40, status=RunStatus.failed)
    with SessionLocal() as db:
        # Resumed today and finished a minute ago: created_at stays 40 days back.
        db.get(WorkflowRun, resumed).finished_at = now().replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    stopping = _finished_run(client, workflow, age_days=40, status=RunStatus.failed)
    with SessionLocal() as db:
        # Cancelled mid-task: the status flips at once, the worker finalizes later.
        row = db.get(WorkflowRun, stopping)
        row.status, row.finished_at = RunStatus.cancelled, None
        db.commit()

    with SessionLocal() as db:
        cleanup_runs(db, older_than_days=30)
    assert _exists(resumed), "a run resumed and finished today was deleted"
    assert _exists(stopping), "a run still being stopped was deleted"


def test_the_newest_successful_run_of_each_workflow_survives(client):
    weekly = make_workflow(client, "monthly-report")
    make_shell_task(client, weekly["id"], "t", emit("ok"))
    older = _finished_run(client, weekly, age_days=60)
    newest = _finished_run(client, weekly, age_days=40)
    failed_since = _finished_run(client, weekly, age_days=35, status=RunStatus.failed)

    with SessionLocal() as db:
        stats = cleanup_runs(db, older_than_days=30)
    assert stats.runs_deleted == 2
    assert not _exists(older) and not _exists(failed_since)
    assert _exists(newest), "the run behind the pinned latest-report link was deleted"


def test_cleanup_removes_rows_and_files_together(client):
    workflow = make_workflow(client, "tidy")
    make_shell_task(client, workflow["id"], "a", emit("a"))
    make_shell_task(client, workflow["id"], "b", emit("b"), depends_on=["a"])
    old = _finished_run(client, workflow, age_days=40)
    _finished_run(client, workflow, age_days=1)
    log_dir = _log_dir(old)
    files = sorted(p for p in log_dir.iterdir())
    assert len(files) == 4  # two tasks, stdout and stderr each

    with SessionLocal() as db:
        dry = cleanup_runs(db, older_than_days=30, dry_run=True)
    assert (dry.runs_deleted, dry.files_deleted) == (1, 4)
    assert all(p.exists() for p in files) and _exists(old), "the dry run deleted something"

    with SessionLocal() as db:
        stats = cleanup_runs(db, older_than_days=30)
    assert (stats.runs_deleted, stats.files_deleted, stats.errors) == (1, 4, [])
    assert not log_dir.exists()
    with SessionLocal() as db:
        for model in (TaskRun, Artifact):
            assert not db.query(model).filter(model.workflow_run_id == old).count(), model


def test_files_that_would_not_delete_are_removed_by_the_next_pass(client, monkeypatch):
    workflow = make_workflow(client, "locked")
    make_shell_task(client, workflow["id"], "t", emit("ok"))
    old = _finished_run(client, workflow, age_days=40)
    _finished_run(client, workflow, age_days=1)

    real_rmtree = shutil.rmtree

    def locked(path, *args, **kwargs):  # what Windows does to a file someone holds open
        raise PermissionError(13, "The process cannot access the file", str(path))

    monkeypatch.setattr(shutil, "rmtree", locked)
    with SessionLocal() as db:
        stats = cleanup_runs(db, older_than_days=30)
    assert stats.runs_deleted == 1 and stats.errors, "the failure was not reported"
    assert _log_dir(old).exists()

    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    with SessionLocal() as db:
        cleanup_runs(db, older_than_days=30)
    assert not _log_dir(old).exists(), "the orphaned directory was never cleaned up"


def test_a_negative_retention_period_is_rejected(monkeypatch):
    """-1 is truthy: it put the cutoff a day in the future and deleted every
    finished run, every six hours. (0 and unset both mean "keep everything".)"""
    monkeypatch.setenv("RUNRAIL_RETENTION_DAYS", "-1")
    with pytest.raises(ValueError):
        Settings()
