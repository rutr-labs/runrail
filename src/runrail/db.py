from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from runrail.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = get_settings().database_url
    if url.startswith("sqlite"):
        # timeout: wait for locks instead of failing when worker threads write concurrently.
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
    elif url.startswith("postgresql"):
        # SQLite drops the offset, so every stored instant is naive UTC and a bare
        # timestamp means UTC throughout the app. A PostgreSQL session otherwise
        # inherits the server's timezone and would read a naive bind — and truncate
        # timestamptz to a day for /stats/daily — as local wall clock. A libpq
        # startup option, not a SET: SET is transactional, and the pool's
        # rollback-on-return would undo it partway through a connection's life.
        kwargs = {"connect_args": {"options": "-c timezone=utc"}}
    else:
        kwargs = {}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL lets readers proceed while one writer commits — required for the
            # concurrent worker pool sharing the database with the API and scheduler.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from runrail import models  # noqa: F401

    get_settings().ensure_directories()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    _restore_point_before_upgrade(config)
    command.upgrade(config, "head")


def _restore_point_before_upgrade(config) -> None:
    """A schema upgrade is the one moment a database can be left in a state
    the previous RunRail cannot read. Before one runs, SQLite homes get a
    snapshot under <home>/backups (the newest few are kept)."""
    import sqlite3
    from pathlib import Path

    from alembic.script import ScriptDirectory

    from runrail import system
    settings = get_settings()
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return
    file = Path(url.split("sqlite:///", 1)[1])
    if not file.exists():
        return
    try:
        with sqlite3.connect(f"file:{file}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return
    head = ScriptDirectory.from_config(config).get_current_head()
    if row and row[0] != head:
        kept = system.restore_point(settings, f"before-{head[:8]}")
        if kept:
            print(f"Restore point before the schema upgrade: {kept}", flush=True)
