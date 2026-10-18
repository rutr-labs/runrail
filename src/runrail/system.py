"""What the Settings page says about this RunRail, and what it can do to it.

Status: version, uptime, where the data is and how big, what the worker and
the supervisor are doing. Controls: pause,
backups, a restore point before every schema upgrade, and a preview of what a
retention setting would delete before it is allowed to.

Anything slow — walking the logs tree — runs on its own thread and the page
shows the last answer with its age; a status request never blocks on the disk.
"""

import contextlib
import io
import json
import logging
import platform
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from runrail import __version__, runtime_settings
from runrail.config import Settings, get_settings
from runrail.models import (
    RunStatus,
    Service,
    ServiceState,
    SupervisorLease,
    Workflow,
    WorkflowRun,
    _aware,
    now,
)

log = logging.getLogger("runrail.system")

STARTED_AT = now()
#: How often the disk is walked for the storage figures.
STORAGE_MAX_AGE = timedelta(minutes=10)
#: Restore points kept under <home>/backups before schema upgrades.
RESTORE_POINTS_KEPT = 3
#: A backup's text members are read into memory; a deflate stream inflates a
#: thousandfold, so the declared size is checked before anything is read.
MAX_MANIFEST_BYTES = 1 << 20
MAX_YAML_BYTES = 64 << 20
#: How long the retention preview may spend counting before it answers with a
#: lower bound.
PREVIEW_BUDGET_SECONDS = 5.0


class _Background:
    """One slow measurement with a cached answer: `get()` returns at once,
    kicking off a refresh in the background when the answer is stale."""

    def __init__(self, work, max_age: timedelta):
        self._work, self._max_age = work, max_age
        self._lock = threading.Lock()
        self._value: Any = None
        self._at: datetime | None = None
        self._thread: threading.Thread | None = None

    def get(self, refresh: bool = False) -> tuple[Any, datetime | None, bool]:
        with self._lock:
            stale = self._at is None or now() - self._at > self._max_age or refresh
            running = self._thread is not None and self._thread.is_alive()
            if stale and not running:
                self._thread = threading.Thread(target=self._refresh, daemon=True,
                                                name="runrail-measure")
                self._thread.start()
                running = True
            return self._value, self._at, running

    def _refresh(self) -> None:
        try:
            value = self._work()
        except Exception:
            log.exception("background measurement failed")
            return
        with self._lock:
            self._value, self._at = value, now()

    def reset(self) -> None:
        with self._lock:
            self._value, self._at = None, None


def _tree_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for file in path.rglob("*"):
            try:
                if file.is_file():
                    total += file.stat().st_size
            except OSError:
                continue
    return total


def _measure_storage() -> dict[str, int]:
    settings = get_settings()
    return {"logs_bytes": _tree_size(settings.logs_dir),
            "artifacts_bytes": _tree_size(settings.artifacts_dir),
            "environments_bytes": _tree_size(settings.environments_dir),
            "backups_bytes": _tree_size(backups_dir(settings))}


# Looked up at call time (not bound at import), so a test can stand in for it.
storage = _Background(lambda: _measure_storage(), STORAGE_MAX_AGE)


def backups_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).home / "backups"


def _database(settings: Settings) -> dict[str, Any]:
    url = settings.database_url
    if url.startswith("sqlite"):
        file = Path(url.split("sqlite:///", 1)[1])
        size = 0
        for candidate in (file, file.with_name(file.name + "-wal"), file.with_name(file.name + "-shm")):
            try:
                size += candidate.stat().st_size
            except OSError:
                pass
        return {"backend": "sqlite", "location": str(file), "size_bytes": size}
    parts = urlsplit(url)
    location = f"{parts.hostname or ''}{':' + str(parts.port) if parts.port else ''}{parts.path}"
    return {"backend": parts.scheme.split("+")[0], "location": location, "size_bytes": None}


def status(db: Session, refresh: bool = False) -> dict[str, Any]:
    settings = get_settings()
    home = settings.home.resolve()
    database = _database(settings)
    if database["backend"] != "sqlite":
        try:
            database["size_bytes"] = db.scalar(text("SELECT pg_database_size(current_database())"))
        except Exception:
            db.rollback()
    sizes, measured_at, measuring = storage.get(refresh)
    counts = {status: count for status, count in db.execute(
        select(WorkflowRun.status, func.count()).where(WorkflowRun.status.in_(
            (RunStatus.running, RunStatus.queued, RunStatus.waiting_approval)))
        .group_by(WorkflowRun.status))}
    lease = db.get(SupervisorLease, "services")
    beat = _aware(lease.heartbeat_at) if lease else None
    from runrail.services.supervisor import LEASE_STALE_AFTER
    supervised = beat is not None and now() - beat < LEASE_STALE_AFTER + timedelta(seconds=5)
    service_states = {state: count for state, count in db.execute(
        select(Service.state, func.count()).group_by(Service.state))}
    at = now()
    return {
        "version": __version__,
        "started_at": STARTED_AT,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "home": str(home),
        "database": database,
        "storage": {**(sizes or {"logs_bytes": None, "artifacts_bytes": None,
                                 "environments_bytes": None, "backups_bytes": None}),
                    "measured_at": measured_at, "measuring": measuring},
        "worker": {"concurrency": runtime_settings.current("worker_concurrency"),
                   "task_parallelism": runtime_settings.current("task_parallelism"),
                   "running": counts.get(RunStatus.running, 0),
                   "queued": counts.get(RunStatus.queued, 0),
                   "waiting_approval": counts.get(RunStatus.waiting_approval, 0),
                   "paused": bool(runtime_settings.current("paused"))},
        "services": {"supervised": supervised, "total": sum(service_states.values()),
                     "running": service_states.get(ServiceState.running, 0)},
        "scheduler": {"next_fire_at": _next_fire(db, at)},
        "retention": {"days": runtime_settings.current("retention_days"),
                      "cleanup": cleanup.snapshot()},
        "now": at,
    }


def _next_fire(db: Session, at: datetime) -> datetime | None:
    from runrail.scheduler.service import _expected_fire
    soonest = None
    for workflow in db.scalars(select(Workflow).where(Workflow.enabled.is_(True),
                                                      Workflow.schedule_cron.is_not(None))):
        fire = _expected_fire(workflow, at)
        if fire is not None and (soonest is None or fire < soonest):
            soonest = fire
    return soonest


# ── retention cleanup, now ─────────────────────────────────────────────────
class _Cleanup:
    """The retention cleanup, one at a time per process, with its progress
    kept for the Settings page. Saving a retention figure starts one at once:
    waiting up to six hours for the scheduler's pass looked like the setting
    had done nothing. The scheduler's own pass goes through here too, so the
    two never run side by side in one process."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False, "days": None, "started_at": None,
                                      "finished_at": None, "runs_deleted": 0,
                                      "files_deleted": 0, "errors": 0}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def _claim(self, days: int) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "days": days, "started_at": now(), "finished_at": None,
                          "runs_deleted": 0, "files_deleted": 0, "errors": 0}
            return True

    def start(self, days: int) -> bool:
        """Begin a cleanup on its own thread; False if one is already running."""
        if not self._claim(days):
            return False
        threading.Thread(target=self.run, args=(days,), name="runrail-retention",
                         daemon=True).start()
        return True

    def run(self, days: int, claimed: bool = True) -> Any:
        """Run a cleanup here and now (the scheduler calls this directly with
        claimed=False). Returns the stats, or None if another one is running."""
        from runrail.db import SessionLocal
        from runrail.maintenance import cleanup_runs
        if not claimed and not self._claim(days):
            return None

        def progress(stats) -> None:
            with self._lock:
                self.state.update(runs_deleted=stats.runs_deleted,
                                  files_deleted=stats.files_deleted, errors=len(stats.errors))
        stats = None
        try:
            with SessionLocal() as db:
                stats = cleanup_runs(db, days, progress=progress)
            progress(stats)
        except Exception:
            log.exception("retention cleanup failed")
            with self._lock:
                self.state["errors"] += 1
        finally:
            with self._lock:
                self.state.update(running=False, finished_at=now())
            storage.reset()  # the home folder just got smaller
        return stats


cleanup = _Cleanup()


# ── retention preview ──────────────────────────────────────────────────────
def retention_preview(db: Session, days: int) -> dict[str, Any]:
    """What `days` would delete right now, without deleting it. `partial`
    means the count stopped at its time budget and the figures are at least."""
    from runrail.maintenance import cleanup_runs
    stats = cleanup_runs(db, days, dry_run=True, budget_seconds=PREVIEW_BUDGET_SECONDS)
    cutoff = now() - timedelta(days=days)
    total = db.scalar(select(func.count()).select_from(WorkflowRun)) or 0
    return {"days": days, "cutoff": cutoff, "runs": stats.runs_deleted,
            "files": stats.files_deleted, "bytes": stats.bytes_freed, "partial": stats.partial,
            "runs_total": total, "runs_kept": total - stats.runs_deleted}


# ── backups ────────────────────────────────────────────────────────────────
def _sqlite_path(settings: Settings) -> Path | None:
    url = settings.database_url
    return Path(url.split("sqlite:///", 1)[1]) if url.startswith("sqlite:///") else None


def _snapshot_sqlite(source: Path, dest: Path) -> None:
    """A consistent copy through SQLite's own backup API: a plain file copy of
    a database in WAL mode misses what is still in the WAL."""
    # closing(): sqlite3's context manager commits but does not close, and an
    # open handle on the file is what makes a later rename fail on Windows.
    with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src, \
            contextlib.closing(sqlite3.connect(dest)) as out:
        src.backup(out)


def _revision(db: Session) -> str | None:
    try:
        return db.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        db.rollback()
        return None


def write_backup(db: Session, out: Path) -> dict[str, Any]:
    """A zip holding the configuration as YAML, the SQLite database (a
    consistent snapshot; PostgreSQL is dumped with pg_dump, not here) and a
    manifest. Logs and artifacts are not in it: they are the bulk of a home
    and sit in plain folders anyone can copy."""
    from runrail.workflow_io import export_workflows
    settings = get_settings()
    exported = export_workflows(db)
    manifest = {
        "created_at": now().isoformat(),
        "runrail_version": __version__,
        "revision": _revision(db),
        "database": _database(settings)["backend"],
        "home": str(settings.home.resolve()),
        "counts": {"workflows": len(exported.get("workflows", [])),
                   "services": len(exported.get("services", [])),
                   "projects": len(exported.get("projects", [])),
                   "environments": len(exported.get("environments", [])),
                   "runs": db.scalar(select(func.count()).select_from(WorkflowRun)) or 0},
    }
    sqlite_file = _sqlite_path(settings)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("workflows.yaml", yaml.safe_dump(exported, sort_keys=False,
                                                          allow_unicode=True))
        if sqlite_file is not None and sqlite_file.exists():
            with tempfile.TemporaryDirectory() as scratch:
                snapshot = Path(scratch) / "runrail.db"
                _snapshot_sqlite(sqlite_file, snapshot)
                archive.write(snapshot, "runrail.db")
            manifest["includes_database"] = True
        else:
            manifest["includes_database"] = False
    return manifest


class BackupError(ValueError):
    pass


def _member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise BackupError(f"{name} in this backup is {info.file_size:,} bytes; "
                          f"the most RunRail will read is {limit:,}")
    return archive.read(name)


def _open_backup(source: bytes | Path) -> tuple[zipfile.ZipFile, dict, dict]:
    """The archive, its manifest and its configuration, or BackupError."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source)
        names = set(archive.namelist())
        if "manifest.json" not in names or "workflows.yaml" not in names:
            raise BackupError("Not a RunRail backup: manifest.json or workflows.yaml is missing")
        manifest = json.loads(_member(archive, "manifest.json", MAX_MANIFEST_BYTES))
        config = yaml.safe_load(_member(archive, "workflows.yaml", MAX_YAML_BYTES)) or {}
    except (zipfile.BadZipFile, OSError):
        raise BackupError("Not a zip file") from None
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise BackupError(f"The backup is damaged: {exc}") from None
    if not isinstance(manifest, dict) or not isinstance(config, dict):
        raise BackupError("The backup's manifest or workflows.yaml does not hold what it should")
    return archive, manifest, config


def inspect_backup(source: bytes | Path) -> dict[str, Any]:
    """The manifest and what the YAML holds, or BackupError saying what is wrong."""
    archive, manifest, config = _open_backup(source)
    return {"manifest": manifest, "includes_database": "runrail.db" in archive.namelist(),
            "configuration": {section: len(config.get(section) or [])
                              for section in ("projects", "environments", "workflows",
                                              "services")}}


def restore_configuration(db: Session, source: bytes | Path) -> dict[str, list[str]]:
    """Apply the backup's YAML — workflows, services, projects, environments —
    the way the Import page does: upsert by key, nothing deleted. History,
    which lives in the database file, is restored offline (`runrail restore`)."""
    from runrail.workflow_io import apply_workflows
    _, _, config = _open_backup(source)
    return apply_workflows(db, config)


def restore_point(settings: Settings, label: str) -> Path | None:
    """A snapshot of the SQLite database under <home>/backups, pruned to the
    newest few. Taken before every schema upgrade, so a bad migration — or a
    downgrade back to the previous RunRail — has something to return to."""
    source = _sqlite_path(settings)
    if source is None or not source.exists():
        return None
    folder = backups_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"runrail-{label}-{stamp}.db"
    try:
        _snapshot_sqlite(source, dest)
    except sqlite3.Error as exc:
        log.warning("could not take a restore point: %s", exc)
        dest.unlink(missing_ok=True)  # a half-written file is not a restore point
        return None
    points = sorted(folder.glob("runrail-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in points[RESTORE_POINTS_KEPT:]:
        try:
            old.unlink()
        except OSError:
            pass
    storage.reset()
    return dest


def list_restore_points(settings: Settings | None = None) -> list[dict[str, Any]]:
    folder = backups_dir(settings)
    if not folder.is_dir():
        return []
    points = sorted(folder.glob("runrail-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size_bytes": p.stat().st_size,
             "created_at": datetime.fromtimestamp(p.stat().st_mtime).astimezone()}
            for p in points]


def server_alive(settings: Settings, within: timedelta = timedelta(seconds=30)) -> bool:
    """Whether a `runrail serve` (or `runrail worker`) wrote the services lease
    lately — the one heartbeat a stopped-server operation can check without
    the API. Honest limitation: `runrail api` or `runrail scheduler` on their
    own write no heartbeat, so the command also says "stop RunRail first"."""
    source = _sqlite_path(settings)
    if source is None or not source.exists():
        return False
    try:
        with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT heartbeat_at FROM supervisor_leases WHERE name='services'"
                               ).fetchone()
    except sqlite3.Error:
        return False
    if not row or not row[0]:
        return False
    try:
        beat = datetime.fromisoformat(str(row[0]))
    except ValueError:
        return False
    if beat.tzinfo is None:
        from datetime import UTC
        beat = beat.replace(tzinfo=UTC)
    return now() - beat < within


def restore_database(archive_path: Path, settings: Settings) -> list[str]:
    """Offline: replace the SQLite database with the one in a backup. Refuses
    while a server is alive, checks the copy's integrity and that its schema
    revision is one this RunRail knows, keeps the current database as a
    restore point, and leaves the normal startup migration to bring the
    restored one up to date."""
    from runrail.importer import known_revisions
    target = _sqlite_path(settings)
    if target is None:
        raise BackupError("Database restore is for SQLite; for PostgreSQL use pg_restore")
    if server_alive(settings):
        raise BackupError("RunRail is running against this home; stop it first")
    target.parent.mkdir(parents=True, exist_ok=True)  # a fresh home on a new machine
    with zipfile.ZipFile(archive_path) as archive:
        if "runrail.db" not in archive.namelist():
            raise BackupError("This backup holds configuration only, no database "
                              "(it was made against PostgreSQL)")
        staging = target.with_name(f".restore-{int(time.time())}.db")
        with archive.open("runrail.db") as src, staging.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    lines = []
    try:
        # The connection is closed before the rename: Windows will not rename
        # a file something still has open.
        with contextlib.closing(sqlite3.connect(staging)) as conn:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if verdict != "ok":
                raise BackupError(f"The backup's database fails its integrity check: {verdict}")
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = row[0] if row else None
        if revision not in known_revisions():
            raise BackupError(f"The backup was made by a newer RunRail (schema {revision}); "
                              "upgrade first")
        kept = restore_point(settings, "before-restore") if target.exists() else None
        if kept:
            lines.append(f"Kept the current database as {kept}")
        # The WAL and SHM belong to the old database; the restore point above
        # already holds everything they contained, so losing them between here
        # and the rename costs nothing.
        for sidecar in (target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
            sidecar.unlink(missing_ok=True)
        staging.replace(target)
        lines.append(f"Restored {target} (schema {revision}); migrations run on the next start")
    finally:
        staging.unlink(missing_ok=True)
    return lines

