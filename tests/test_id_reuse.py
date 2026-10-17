"""Run ids are permalinks, so SQLite must never hand one out twice.

Without AUTOINCREMENT, SQLite assigns max(rowid) + 1: after retention deleted
the newest runs, their ids came back, and an old link to /runs/<id> or its
report opened somebody else's run. The migration rebuilds three tables to add
it, which is only safe because the migration connection runs without foreign
keys — with them on, dropping the old table cascades into every child row.
This upgrades a populated database to prove the rows survive the rebuild.
"""

import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from conftest import TEST_DB_URL

pytestmark = pytest.mark.skipif(bool(TEST_DB_URL), reason="SQLite's rowid allocation")


def _config(url: str) -> Config:
    from pathlib import Path

    import runrail.db
    config = Config()
    config.set_main_option("script_location",
                           str(Path(runrail.db.__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_upgrading_keeps_every_row_and_stops_reusing_ids(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    url = f"sqlite:///{path}"
    monkeypatch.setenv("RUNRAIL_DB_URL", url)
    monkeypatch.setenv("RUNRAIL_HOME", str(tmp_path / "home"))
    from runrail.config import get_settings
    get_settings.cache_clear()
    command.upgrade(_config(url), "n5b2d81f3a94")  # the revision before this one

    # The workflow through the table as it is at that revision (later ones add
    # columns the model has); the rest through the models, whose tables match.
    from datetime import datetime

    from sqlalchemy import MetaData, Table, create_engine, insert
    from sqlalchemy.orm import Session

    from runrail.models import (
        Artifact,
        ArtifactType,
        RunStatus,
        Task,
        TaskRun,
        TaskRunStatus,
        TaskType,
        TriggerType,
        WorkflowRun,
    )
    engine = create_engine(url)
    workflows = Table("workflows", MetaData(), autoload_with=engine)
    stamp = datetime(2026, 1, 1)
    with engine.begin() as connection:
        workflow_id = connection.execute(insert(workflows).values(
            name="w", enabled=True, max_concurrent_runs=1, lock_mode="shared",
            snooze_pauses_runs=False, created_at=stamp,
            updated_at=stamp)).inserted_primary_key[0]
    with Session(engine) as db:
        task = Task(workflow_id=workflow_id, name="t", task_type=TaskType.shell, command="exit 0")
        db.add(task); db.flush()
        for _ in range(3):
            run = WorkflowRun(workflow_id=workflow_id, status=RunStatus.success,
                              trigger_type=TriggerType.manual)
            db.add(run); db.flush()
            task_run = TaskRun(workflow_run_id=run.id, task_id=task.id, attempt=1,
                               status=TaskRunStatus.success, resume_index=0)
            db.add(task_run); db.flush()
            db.add(Artifact(task_run_id=task_run.id, workflow_run_id=run.id, name="a",
                            artifact_type=ArtifactType.notebook, path="p"))
        db.commit()
    engine.dispose()

    command.upgrade(_config(url), "head")
    get_settings.cache_clear()

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        counts = [db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("workflow_runs", "task_runs", "artifacts")]
        assert counts == [3, 3, 3], "the table rebuild lost rows"
        # The next migration gave the existing workflow its YAML key.
        assert len(db.execute("SELECT key FROM workflows").fetchone()[0]) == 32
        # Retention deletes the newest run; its children cascade, as before.
        db.execute("DELETE FROM workflow_runs WHERE id = 3")
        assert db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 2
        reused = db.execute("INSERT INTO workflow_runs (workflow_id, status, trigger_type, "
                            "created_at, resume_count) VALUES (1, 'queued', 'manual', "
                            "'2026-01-02', 0) RETURNING id").fetchone()[0]
    assert reused == 4, "a deleted run's id was handed out again"
