import os
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


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RUNRAIL_HOME", str(tmp_path / ".runrail"))
    monkeypatch.setenv("RUNRAIL_DB_URL", TEST_DB_URL or f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("RUNRAIL_BROWSE_ROOT", str(tmp_path))
    from runrail.config import get_settings
    get_settings.cache_clear()
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
