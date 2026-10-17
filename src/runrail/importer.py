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

Everything is assembled in a staging directory inside the destination and moved
into place only once all of it is there. An import used to copy the database
first and the data trees after, so a failure part-way (a full disk, a broken
symlink in a virtualenv, a database from a newer RunRail) left a home with a
database and no logs — which the next start treated as a real home and a
retry refused to overwrite.
"""

import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

from runrail.config import Settings

DATA_DIRS = ("logs", "artifacts", "environments")
DB_SIDECARS = ("runrail.db-wal", "runrail.db-shm")

#: Columns holding absolute paths inside the home, rewritten to the destination
#: on import. Left alone, every log and artifact of the imported history kept
#: pointing at the old home: unreadable once it was deleted, and — worse —
#: deleted there by the new home's retention while the copies survived.
_PATH_COLUMNS = (
    ("task_runs", "stdout_log_path"), ("task_runs", "stderr_log_path"),
    ("artifacts", "path"), ("service_sessions", "log_path"),
    ("environments", "executable"),
)


class ImportSourceError(ValueError):
    """The requested import source can't be used; the message says why."""


def is_fresh_home(settings: Settings) -> bool:
    """True when nothing exists to lose: default SQLite and no database file yet."""
    return settings.db_url is None and not (settings.home / "runrail.db").exists()


def _read_only(db_file: Path) -> sqlite3.Connection:
    # as_uri(), not an f-string: a path with `?`, `#` or `%` in it, or a
    # Windows drive, is not a valid URI as plain text.
    return sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)


def known_revisions() -> set[str]:
    """Every migration revision this install ships."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import runrail.db
    config = Config()
    config.set_main_option("script_location", str(Path(runrail.db.__file__).parent / "migrations"))
    return {script.revision for script in ScriptDirectory.from_config(config).walk_revisions()}


def validate_source_home(source: Path) -> Path:
    """Check that ``source`` is an importable RunRail data directory.

    Verifies the marker database exists, passes SQLite's quick_check while
    opened read-only, and was written by a RunRail this install can upgrade
    from — so a corrupt, half-written or newer database is rejected before
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
        conn = _read_only(db_file)
        try:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
            revision = _revision(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ImportSourceError(f"Could not read {db_file}: {exc}") from exc
    if verdict != "ok":
        raise ImportSourceError(
            f"{db_file} failed an integrity check ({verdict}); "
            "not importing a damaged database."
        )
    if revision is not None and revision not in known_revisions():
        raise ImportSourceError(
            f"{db_file} was written by a newer RunRail (schema {revision}) than this one. "
            "Upgrade RunRail here first, then import."
        )
    return source


def _revision(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


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
    # Any of the three: a WAL or SHM file left behind beside a moved-aside
    # database would be paired with the imported one, which is how SQLite
    # databases get corrupted.
    leftover = [name for name in ("runrail.db", *DB_SIDECARS) if (dest / name).exists()]
    if leftover:
        raise ImportSourceError(
            f"{dest} already contains {', '.join(leftover)}; refusing to overwrite. "
            "Move all of them aside first if you really mean to replace them."
        )
    occupied = [name for name in DATA_DIRS if (dest / name).is_dir() and any((dest / name).iterdir())]
    if occupied:
        raise ImportSourceError(
            f"{dest} already has files under {', '.join(occupied)}; "
            "import only into an empty home.")

    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / f".import-{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    try:
        lines = _assemble(source, staging, progress)
        # Everything is here; now, and only now, it becomes the home.
        for name in ("runrail.db", *DATA_DIRS):
            if (staging / name).exists():
                if (dest / name).is_dir():
                    (dest / name).rmdir()  # empty, checked above
                os.replace(staging / name, dest / name)
    except ImportSourceError:
        raise
    except (OSError, sqlite3.Error, shutil.Error) as exc:
        raise ImportSourceError(f"Import failed part-way and nothing was kept: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return lines


def _assemble(source: Path, staging: Path, progress: Callable[[str], None]) -> list[str]:
    progress("copying database…")
    # SQLite's own backup, not a file copy: a copy of a database another
    # RunRail is still writing (its worker, its scheduler) can be torn between
    # the main file and its WAL. The backup reads a consistent snapshot.
    src = _read_only(source / "runrail.db")
    dst = sqlite3.connect(staging / "runrail.db")
    try:
        src.backup(dst)
        verdict = dst.execute("PRAGMA quick_check").fetchone()[0]
        if verdict != "ok":
            raise ImportSourceError(f"the copied database failed an integrity check ({verdict})")
        rewritten = _rewrite_paths(dst, source, staging.parent)
        dst.commit()
    finally:
        dst.close()
        src.close()

    size_mb = (staging / "runrail.db").stat().st_size / 1_000_000
    lines = [f"database: copied ({size_mb:.1f} MB)"]
    copied_environments = False
    for name in DATA_DIRS:
        tree = source / name
        if not tree.is_dir():
            continue
        tree_mb = sum(p.stat().st_size for p in tree.rglob("*") if p.is_file()) / 1_000_000
        progress(f"copying {name} ({tree_mb:.0f} MB)…"
                 + (" managed virtualenvs make this the slow part"
                    if name == "environments" and tree_mb > 100 else ""))
        # symlinks=True: a virtualenv's bin/python is a symlink to the base
        # interpreter, and copying it as a file would turn an environment into
        # a second copy of Python — or fail outright on a link that dangles.
        shutil.copytree(tree, staging / name, symlinks=True)
        files = sum(1 for p in (staging / name).rglob("*") if p.is_file())
        lines.append(f"{name}: {files} file(s)")
        copied_environments = copied_environments or (name == "environments" and files > 0)
    if rewritten:
        lines.append(f"paths: {rewritten} record(s) re-pointed at the new home")

    workflows = _count_workflows(staging / "runrail.db")
    if workflows is not None:
        lines.append(f"workflows: {workflows}")
    if copied_environments:
        lines.append(
            "note: managed environments were copied as-is; if a task fails to "
            "start, rebuild its environment from the Environments page."
        )
    return lines


def _rewrite_paths(conn: sqlite3.Connection, source: Path, dest: Path) -> int:
    """Re-point every stored absolute path inside the old home at the new one.
    Returns how many records changed."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # The paths were stored with the source resolved on its own machine; try
    # the directory as given too, in case it was reached through a symlink.
    prefixes = {str(source), str(source.resolve())}
    changed = 0
    for table, column in _PATH_COLUMNS:
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            continue
        for prefix in prefixes:
            old = prefix.rstrip("/\\") + os.sep
            new = str(dest).rstrip("/\\") + os.sep
            cursor = conn.execute(
                f"UPDATE {table} SET {column} = ? || substr({column}, ?) "
                f"WHERE {column} LIKE ? ESCAPE '!'",
                (new, len(old) + 1, _like(old) + "%"))
            changed += cursor.rowcount
    return changed


def _like(text: str) -> str:
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _count_workflows(db_file: Path) -> int | None:
    """Decorative count for the import summary; None when it can't be read."""
    try:
        conn = _read_only(db_file)
        try:
            return conn.execute("SELECT count(*) FROM workflows").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
