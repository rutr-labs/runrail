"""The System side of Settings: status that never blocks, backups that
restore, a restore point before every schema upgrade, and a retention
preview that tells the truth before anything is deleted."""

import io
import sqlite3
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import TEST_DB_URL
from test_interactions import make_shell_task, make_workflow

from runrail import system
from runrail.config import Settings, get_settings
from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, WorkflowRun


def test_status_reports_the_machine_and_the_work(client, monkeypatch):
    monkeypatch.setattr(system, "_ask_pypi", lambda: {"version": "99.0.0", "error": None})
    system.latest_release.reset(); system.storage.reset()
    make_workflow(client, "nightly", schedule_cron="0 3 * * *")
    first = client.get("/api/system/status").json()
    assert first["version"] and first["home"] == str(get_settings().home.resolve())
    assert first["database"]["backend"] in ("sqlite", "postgresql")
    assert first["disk"]["free_bytes"] > 0
    assert first["worker"] == {"concurrency": 4, "task_parallelism": 4, "running": 0, "queued": 0,
                               "waiting_approval": 0, "paused": False}
    assert first["services"] == {"supervised": False, "total": 0, "running": 0}
    assert first["scheduler"]["next_fire_at"] is not None
    # Slow figures arrive from the background; the request did not wait for them.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        later = client.get("/api/system/status").json()
        if later["storage"]["logs_bytes"] is not None and later["update"]["latest"]:
            break
        time.sleep(0.1)
    assert later["storage"]["measured_at"] and later["storage"]["logs_bytes"] >= 0
    assert later["update"] == {"latest": "99.0.0", "available": True, "error": None,
                               "checked_at": later["update"]["checked_at"], "enabled": True}
    # Switched off, PyPI is not asked and nothing is claimed.
    client.put("/api/settings", json={"update_check": False})
    assert client.get("/api/system/status").json()["update"]["latest"] is None


def test_the_retention_preview_counts_what_a_setting_would_delete(client):
    workflow = make_workflow(client, "w")
    logs = get_settings().logs_dir
    with SessionLocal() as db:
        for days_ago in (90, 60, 2):
            run = WorkflowRun(workflow_id=workflow["id"], status=RunStatus.failed,
                              trigger_type=TriggerType.manual,
                              created_at=datetime.now(UTC) - timedelta(days=days_ago),
                              finished_at=datetime.now(UTC) - timedelta(days=days_ago))
            db.add(run); db.flush()
            folder = logs / f"run_{run.id}"
            folder.mkdir(parents=True)
            (folder / "out.log").write_bytes(b"x" * 1000)
        db.commit()
    preview = client.get("/api/system/retention-preview?days=30").json()
    assert (preview["runs"], preview["files"], preview["bytes"]) == (2, 2, 2000)
    assert preview["runs_total"] == 3 and preview["runs_kept"] == 1
    assert client.get("/api/system/retention-preview?days=-1").status_code == 422
    assert len(client.get("/api/runs?limit=10").json()) == 3, "a preview deleted something"


def test_a_backup_round_trips_the_configuration(client):
    workflow = make_workflow(client, "etl")
    make_shell_task(client, workflow["id"], "t", "echo")
    client.post("/api/services", json={"name": "model", "command": "serve"})
    response = client.get("/api/system/backup")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    data = response.content
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = set(archive.namelist())
    assert {"manifest.json", "workflows.yaml"} <= names
    if not TEST_DB_URL:
        assert "runrail.db" in names
        with sqlite3.connect(":memory:") as probe:
            probe.deserialize(archive.read("runrail.db")) if hasattr(probe, "deserialize") else None
    assert client.post("/api/system/restore/preview", content=data).status_code == 415
    zipped = {"Content-Type": "application/zip"}
    preview = client.post("/api/system/restore/preview", content=data, headers=zipped).json()
    assert preview["configuration"]["workflows"] == 1 and preview["configuration"]["services"] == 1
    assert preview["manifest"]["counts"]["workflows"] == 1

    assert client.delete(f"/api/workflows/{workflow['id']}").status_code == 204
    restored = client.post("/api/system/restore", content=data, headers=zipped).json()["summary"]
    assert "etl" in restored["created"]
    assert [w["name"] for w in client.get("/api/workflows").json()] == ["etl"]
    assert client.post("/api/system/restore", content=b"not a zip", headers=zipped).status_code == 422


def test_a_backup_whose_members_claim_to_be_huge_is_not_read(client):
    """A deflate stream inflates a thousandfold; the declared size is checked
    before a member is read into memory."""
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("workflows.yaml", b"\0" * (system.MAX_YAML_BYTES + 1))
    response = client.post("/api/system/restore/preview", content=bomb.getvalue(),
                           headers={"Content-Type": "application/zip"})
    assert response.status_code == 422 and "the most RunRail will read" in response.json()["detail"]


@pytest.mark.skipif(bool(TEST_DB_URL), reason="restore points are for SQLite homes")
def test_a_restore_point_is_taken_before_a_schema_upgrade(tmp_path, monkeypatch, capsys):
    from alembic import command
    from alembic.config import Config

    import runrail.db
    from runrail.db import init_db
    home = tmp_path / "home"
    home.mkdir()
    url = f"sqlite:///{home / 'runrail.db'}"
    monkeypatch.setenv("RUNRAIL_HOME", str(home))
    monkeypatch.setenv("RUNRAIL_DB_URL", url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(runrail.db.__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "t1c5e8b3d7a2")  # one behind head
    init_db()
    points = system.list_restore_points(get_settings())
    assert len(points) == 1 and points[0]["name"].startswith("runrail-before-")
    assert "Restore point" in capsys.readouterr().out
    init_db()  # already at head: nothing new
    assert len(system.list_restore_points(get_settings())) == 1
    for n in range(5):  # the newest few are kept
        system.restore_point(get_settings(), f"p{n}")
    assert len(system.list_restore_points(get_settings())) == system.RESTORE_POINTS_KEPT
    # A snapshot that fails part-way is not left behind as if it were one.
    monkeypatch.setattr(system, "_snapshot_sqlite",
                        lambda source, dest: (dest.write_bytes(b"half"), (_ for _ in ()).throw(
                            sqlite3.OperationalError("disk full"))))
    assert system.restore_point(get_settings(), "broken") is None
    assert not any("broken" in p["name"] for p in system.list_restore_points(get_settings()))
    get_settings.cache_clear()


@pytest.mark.skipif(bool(TEST_DB_URL), reason="database restore is for SQLite homes")
def test_restoring_a_database_offline_swaps_the_file_and_keeps_the_old_one(client, tmp_path):
    """Backup, lose a workflow, restore: the backup's database is back in
    place, the one it replaced is a restore point, and a running server would
    have been refused."""
    workflow = make_workflow(client, "precious")
    archive = tmp_path / "b.zip"
    with SessionLocal() as db:
        system.write_backup(db, archive)
    assert client.delete(f"/api/workflows/{workflow['id']}").status_code == 204
    settings = get_settings()
    db_file = system._sqlite_path(settings)
    # Another engine must not be holding the file: close ours.
    from runrail import db as database
    database.engine.dispose()

    lines = system.restore_database(archive, settings)
    assert any("Restored" in line for line in lines)
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT name FROM workflows").fetchall() == [("precious",)]
    assert len(system.list_restore_points(settings)) == 1

    # A live server (a fresh supervisor heartbeat) blocks it.
    with sqlite3.connect(db_file) as conn:
        conn.execute("INSERT OR REPLACE INTO supervisor_leases (name, owner, heartbeat_at) "
                     "VALUES ('services', 'x', ?)", (datetime.now(UTC).replace(tzinfo=None),))
    with pytest.raises(system.BackupError, match="running"):
        system.restore_database(archive, settings)

    # Onto a machine that has never run RunRail: the home does not exist yet.
    fresh = Settings(home=tmp_path / "new-machine" / "home", db_url=None)
    assert any("Restored" in line for line in system.restore_database(archive, fresh))
    with sqlite3.connect(system._sqlite_path(fresh)) as conn:
        assert conn.execute("SELECT name FROM workflows").fetchall() == [("precious",)]
