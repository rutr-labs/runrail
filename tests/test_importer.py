"""Importing a previous RunRail data directory into a fresh home."""

import sqlite3
from pathlib import Path

import pytest

from runrail.importer import ImportSourceError, import_home, is_fresh_home


def make_source_home(root: Path, workflows: int = 2) -> Path:
    """A minimal but genuine previous home: valid SQLite db + data trees."""
    src = root / "old-runrail"
    src.mkdir()
    with sqlite3.connect(src / "runrail.db") as conn:
        conn.execute("CREATE TABLE workflows (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO workflows (name) VALUES (?)",
                         [(f"wf-{i}",) for i in range(workflows)])
    (src / "logs" / "run_1").mkdir(parents=True)
    (src / "logs" / "run_1" / "task.stdout.log").write_text("hello\n")
    (src / "artifacts" / "1").mkdir(parents=True)
    (src / "artifacts" / "1" / "report.txt").write_text("data")
    return src


def test_import_home_copies_database_and_data_dirs(tmp_path: Path):
    src = make_source_home(tmp_path)
    dest = tmp_path / "new-home"

    lines = import_home(src, dest)

    with sqlite3.connect(f"file:{dest / 'runrail.db'}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 2
    assert (dest / "logs" / "run_1" / "task.stdout.log").read_text() == "hello\n"
    assert (dest / "artifacts" / "1" / "report.txt").read_text() == "data"
    summary = "\n".join(lines)
    assert "database" in summary and "workflows: 2" in summary


def test_import_home_refuses_to_overwrite_an_existing_database(tmp_path: Path):
    src = make_source_home(tmp_path)
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "runrail.db").write_bytes(b"existing")

    with pytest.raises(ImportSourceError, match="refusing to overwrite"):
        import_home(src, dest)
    assert (dest / "runrail.db").read_bytes() == b"existing"  # untouched


def test_import_home_rejects_a_directory_without_a_database(tmp_path: Path):
    not_a_home = tmp_path / "random-dir"
    not_a_home.mkdir()
    with pytest.raises(ImportSourceError, match="does not look like a RunRail data directory"):
        import_home(not_a_home, tmp_path / "dest")


def test_import_home_rejects_a_corrupt_database(tmp_path: Path):
    src = tmp_path / "damaged"
    src.mkdir()
    (src / "runrail.db").write_bytes(b"this is not sqlite at all" * 10)
    with pytest.raises(ImportSourceError):
        import_home(src, tmp_path / "dest")


def test_import_home_rejects_importing_a_home_into_itself(tmp_path: Path):
    src = make_source_home(tmp_path)
    with pytest.raises(ImportSourceError, match="same directory"):
        import_home(src, src)


def test_is_fresh_home_flips_once_a_database_exists(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RUNRAIL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RUNRAIL_DB_URL", raising=False)
    from runrail.config import Settings
    settings = Settings()
    assert is_fresh_home(settings)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "runrail.db").touch()
    assert not is_fresh_home(settings)
    # An external database means nothing local to import over — never "fresh".
    assert not is_fresh_home(Settings(db_url="postgresql+psycopg://x/y"))


def _rows(db_file: Path, sql: str):
    with sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True) as conn:
        return conn.execute(sql).fetchall()


def make_runrail_home(root: Path, monkeypatch) -> Path:
    """A home shaped like a real one: the schema RunRail writes, rows pointing
    at files inside it, and a managed environment under it."""
    monkeypatch.setenv("RUNRAIL_HOME", str(root / "old"))
    monkeypatch.setenv("RUNRAIL_DB_URL", f"sqlite:///{root / 'old' / 'runrail.db'}")
    from runrail.config import get_settings
    get_settings.cache_clear()
    import runrail.db as database
    database.engine.dispose()
    database.engine = database._make_engine()
    database.SessionLocal.configure(bind=database.engine)
    database.init_db()
    from runrail.models import (
        Artifact,
        ArtifactType,
        Environment,
        RunStatus,
        Task,
        TaskRun,
        TaskRunStatus,
        TaskType,
        TriggerType,
        Workflow,
        WorkflowRun,
    )
    src = (root / "old").resolve()
    (src / "logs" / "run_1").mkdir(parents=True)
    (src / "logs" / "run_1" / "task_run_1.stdout.log").write_text("hello\n")
    (src / "environments" / "env-1-abc12345" / "bin").mkdir(parents=True)
    with database.SessionLocal() as db:
        env = Environment(name="e", managed=True, status="ready",
                          executable=str(src / "environments" / "env-1-abc12345" / "bin" / "python"))
        wf = Workflow(name="w"); db.add_all([env, wf]); db.flush()
        task = Task(workflow_id=wf.id, name="t", task_type=TaskType.shell, command="echo")
        db.add(task); db.flush()
        run = WorkflowRun(workflow_id=wf.id, status=RunStatus.success, trigger_type=TriggerType.manual)
        db.add(run); db.flush()
        tr = TaskRun(workflow_run_id=run.id, task_id=task.id, attempt=1, status=TaskRunStatus.success,
                     resume_index=0, stdout_log_path=str(src / "logs" / "run_1" / "task_run_1.stdout.log"),
                     stderr_log_path=str(src / "logs" / "run_1" / "task_run_1.stderr.log"))
        db.add(tr); db.flush()
        db.add(Artifact(task_run_id=tr.id, workflow_run_id=run.id, name="a", artifact_type=ArtifactType.html,
                        path=str(src / "artifacts" / "1" / "a.html")))
        db.commit()
    database.engine.dispose()
    return src


def test_imported_rows_point_at_the_new_home(tmp_path: Path, monkeypatch):
    """Rows kept the old home's absolute paths: unreadable once it was gone, and
    deleted there by the new home's retention while the copies survived."""
    src = make_runrail_home(tmp_path, monkeypatch)
    dest = (tmp_path / "new").resolve()
    lines = import_home(src, dest)
    assert any(line.startswith("paths: ") for line in lines)
    (stdout, stderr), = _rows(dest / "runrail.db", "SELECT stdout_log_path, stderr_log_path FROM task_runs")
    assert stdout == str(dest / "logs" / "run_1" / "task_run_1.stdout.log")
    assert Path(stdout).read_text() == "hello\n"
    assert stderr.startswith(str(dest))
    (artifact,), = _rows(dest / "runrail.db", "SELECT path FROM artifacts")
    assert artifact == str(dest / "artifacts" / "1" / "a.html")
    (executable,), = _rows(dest / "runrail.db", "SELECT executable FROM environments")
    assert executable == str(dest / "environments" / "env-1-abc12345" / "bin" / "python")


def test_a_failure_part_way_leaves_no_half_home(tmp_path: Path, monkeypatch):
    """The database used to be copied first and the trees after, so a copy that
    failed left a home with a database and no logs — which the next start
    treated as real, and a retry refused to overwrite."""
    import shutil

    src = make_source_home(tmp_path)
    dest = tmp_path / "new-home"
    real = shutil.copytree

    def failing(source, target, *args, **kwargs):
        if Path(source).name == "artifacts":
            raise OSError(28, "No space left on device")
        return real(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", failing)
    with pytest.raises(ImportSourceError, match="nothing was kept"):
        import_home(src, dest)
    assert not (dest / "runrail.db").exists()
    assert not (dest / "logs").exists()
    assert not any(p.name.startswith(".import-") for p in dest.iterdir())
    # And the retry works.
    monkeypatch.setattr(shutil, "copytree", real)
    import_home(src, dest)
    assert (dest / "runrail.db").exists()


def test_a_home_from_a_newer_runrail_is_refused_before_anything_is_copied(tmp_path: Path):
    src = make_source_home(tmp_path)
    with sqlite3.connect(src / "runrail.db") as conn:
        conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
        conn.execute("INSERT INTO alembic_version VALUES ('zz9future0000')")
    dest = tmp_path / "new-home"
    with pytest.raises(ImportSourceError, match="newer RunRail"):
        import_home(src, dest)
    assert not dest.exists() or not any(dest.iterdir())


def test_a_stale_wal_file_in_the_destination_is_refused(tmp_path: Path):
    """A WAL left beside a moved-aside database would be paired with the
    imported one — SQLite's documented route to corruption."""
    src = make_source_home(tmp_path)
    dest = tmp_path / "new-home"
    dest.mkdir()
    (dest / "runrail.db-wal").write_bytes(b"stale")
    with pytest.raises(ImportSourceError, match="runrail.db-wal"):
        import_home(src, dest)
