import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

#: The same suite runs against either shipped backend. Unset is the SQLite file
#: every local run and the default deployment use; CI's second backend job sets
#: it to a PostgreSQL URL.
TEST_DB_URL = os.environ.get("RUNRAIL_TEST_DB_URL")


def _reset(engine) -> None:
    """Empty a backend that outlives the test.

    SQLite gets a fresh file per test from tmp_path, so only a server-backed URL
    needs this. RESTART IDENTITY because sequences are not part of the schema the
    migrations rebuild, and tests read ids back.
    """
    if engine.dialect.name == "sqlite":
        return
    from runrail.db import Base
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session")
def _migrated_sqlite(tmp_path_factory) -> Path | None:
    """A SQLite database at the head of the migration chain, built once.

    Every test used to run the whole chain into its own fresh file: about
    0.2s a test here and 1.7s on the Windows runner, most of that job's eight
    minutes. Each test now gets a copy of this one. The chain itself still runs
    for real — this is it — and init_db still runs per test against the copy,
    where it finds nothing left to do.
    """
    if TEST_DB_URL:
        return None
    from runrail.config import get_settings
    from runrail.db import init_db
    directory = tmp_path_factory.mktemp("migrated")
    template = directory / "template.db"
    saved = {key: os.environ.get(key) for key in ("RUNRAIL_HOME", "RUNRAIL_DB_URL")}
    os.environ["RUNRAIL_HOME"] = str(directory / ".runrail")
    os.environ["RUNRAIL_DB_URL"] = f"sqlite:///{template}"
    get_settings.cache_clear()
    try:
        init_db()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
    return template


@pytest.fixture()
def client(tmp_path: Path, monkeypatch, _migrated_sqlite):
    monkeypatch.setenv("RUNRAIL_HOME", str(tmp_path / ".runrail"))
    monkeypatch.setenv("RUNRAIL_DB_URL", TEST_DB_URL or f"sqlite:///{tmp_path / 'test.db'}")
    if _migrated_sqlite is not None:
        shutil.copyfile(_migrated_sqlite, tmp_path / "test.db")
    monkeypatch.setenv("RUNRAIL_BROWSE_ROOT", str(tmp_path))
    from runrail.config import get_settings
    get_settings.cache_clear()
    # Settings saved from the page are cached per process; a test must not
    # inherit what the previous one saved.
    from runrail import runtime_settings, system
    runtime_settings.invalidate()
    # The update check asks PyPI on a thread; no test should reach the network.
    monkeypatch.setattr(system, "_ask_pypi", lambda: {"version": None, "error": "offline (test)"})
    system.latest_release.reset()
    import runrail.environments as environments
    monkeypatch.setattr(environments, "_RUNTIME_PACKAGES", ())
    import runrail.db as database
    database.engine.dispose()
    database.engine = database._make_engine()
    database.SessionLocal.configure(bind=database.engine)
    database.init_db()
    _reset(database.engine)
    from runrail.api.app import create_app
    with TestClient(create_app()) as test_client:
        yield test_client
    database.engine.dispose()
