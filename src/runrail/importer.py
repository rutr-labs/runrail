"""Bring an existing RunRail setup into the current home.

Covers the moment the home directory moves — a new machine, a changed
RUNRAIL_HOME, or a packaged install using the default location — so starting
over never strands data. Two source shapes are supported:

- a previous RunRail data directory (its ``runrail.db`` plus the ``logs``,
  ``artifacts``, and ``environments`` trees are copied; the normal startup
  migrations then upgrade the copied database in place), or
- a workflows YAML produced by ``runrail export`` (applied declaratively).

The CLI offers this interactively the first time ``runrail serve`` targets an
empty home, and any time via ``runrail import``.
"""

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from runrail.config import Settings

DATA_DIRS = ("logs", "artifacts", "environments")
DB_SIDECARS = ("runrail.db-wal", "runrail.db-shm")


class ImportSourceError(ValueError):
    """The requested import source can't be used; the message says why."""


def is_fresh_home(settings: Settings) -> bool:
    """True when nothing exists to lose: default SQLite and no database file yet."""
    return settings.db_url is None and not (settings.home / "runrail.db").exists()


def validate_source_home(source: Path) -> Path:
    """Check that ``source`` is an importable RunRail data directory.

    Verifies the marker database exists and passes SQLite's quick_check while
    opened read-only, so a corrupt or half-written file is rejected before
    anything is copied.
    """
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ImportSourceError(f"{source} is not a directory")
    db_file = source / "runrail.db"
    if not db_file.is_file():
        raise ImportSourceError(
            f"{source} does not look like a RunRail data directory (no runrail.db). "
            "If that setup uses PostgreSQL, point RUNRAIL_DB_URL at it instead — "
            "no import needed."
        )
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ImportSourceError(f"Could not read {db_file}: {exc}") from exc
    if verdict != "ok":
        raise ImportSourceError(
            f"{db_file} failed an integrity check ({verdict}); "
            "not importing a damaged database."
        )
    return source


def import_home(source: Path, dest: Path,
                on_progress: Callable[[str], None] | None = None) -> list[str]:
    """Copy a previous RunRail data directory into ``dest``; return summary lines.

    Refuses to overwrite an existing database — the import targets fresh homes
    only. The copied database is left for the normal startup migrations to
    upgrade, which is what keeps old versions importable. ``on_progress`` gets
    a line per copy step; environments trees hold entire virtualenvs, so a
    silent import can look hung.
    """
    progress = on_progress or (lambda _line: None)
    source = validate_source_home(source)
    dest = dest.expanduser().resolve()
    if source == dest:
        raise ImportSourceError("Source and destination are the same directory")
    if (dest / "runrail.db").exists():
        raise ImportSourceError(
            f"{dest} already contains a database; refusing to overwrite it. "
            "Move it aside first if you really mean to replace it."
        )

    dest.mkdir(parents=True, exist_ok=True)
    progress("copying database…")
    shutil.copy2(source / "runrail.db", dest / "runrail.db")
    for sidecar in DB_SIDECARS:  # keep WAL/SHM together so no commits are lost
        if (source / sidecar).exists():
            shutil.copy2(source / sidecar, dest / sidecar)

    size_mb = (dest / "runrail.db").stat().st_size / 1_000_000
    lines = [f"database: copied ({size_mb:.1f} MB)"]
    copied_environments = False
    for name in DATA_DIRS:
        tree = source / name
        if not tree.is_dir():
            continue
        tree_mb = sum(p.stat().st_size for p in tree.rglob("*") if p.is_file()) / 1_000_000
        progress(f"copying {name} ({tree_mb:.0f} MB)…"
                 + (" managed virtualenvs make this the slow part" if name == "environments" and tree_mb > 100 else ""))
        shutil.copytree(tree, dest / name, dirs_exist_ok=True)
        files = sum(1 for p in (dest / name).rglob("*") if p.is_file())
        lines.append(f"{name}: {files} file(s)")
        copied_environments = copied_environments or (name == "environments" and files > 0)

    workflows = _count_workflows(dest / "runrail.db")
    if workflows is not None:
        lines.append(f"workflows: {workflows}")
    if copied_environments:
        lines.append(
            "note: managed environments were copied as-is; if a task fails to "
            "start, rebuild its environment from the Environments page."
        )
    return lines


def _count_workflows(db_file: Path) -> int | None:
    """Decorative count for the import summary; None when it can't be read."""
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT count(*) FROM workflows").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
