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
