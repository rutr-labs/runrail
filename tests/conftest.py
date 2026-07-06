from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RUNRAIL_HOME", str(tmp_path / ".runrail"))
    monkeypatch.setenv("RUNRAIL_DB_URL", f"sqlite:///{tmp_path / 'test.db'}")
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
    from runrail.api.app import create_app
    with TestClient(create_app()) as test_client:
        yield test_client
    database.engine.dispose()
