"""Regression coverage for the Windows rename-lock fix in provision_managed.

WinError 5 (access denied) on the staging->target directory rename is a routine
occurrence on Windows when the environments folder sits under OneDrive/antivirus:
those processes transiently lock the many small files a fresh `pip install` just
wrote. _replace_directory must retry through that window and fall back to a
copy+delete rather than surfacing the failure immediately.
"""

from pathlib import Path

import pytest

from runrail.environments import _replace_directory


def test_replace_directory_succeeds_on_first_try(tmp_path: Path):
    src = tmp_path / "src"; src.mkdir(); (src / "file.txt").write_text("hi")
    dst = tmp_path / "dst"
    _replace_directory(src, dst)
    assert not src.exists()
    assert (dst / "file.txt").read_text() == "hi"


def test_replace_directory_retries_transient_lock_then_succeeds(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"; src.mkdir(); (src / "file.txt").write_text("hi")
    dst = tmp_path / "dst"

    real_rename = Path.rename
    calls = {"count": 0}

    def flaky_rename(self, target):
        calls["count"] += 1
        if calls["count"] < 3:
            raise OSError(5, "Access is denied")  # simulates OneDrive/AV lock
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    monkeypatch.setattr("runrail.environments.time.sleep", lambda _: None)
    _replace_directory(src, dst, attempts=5, delay=0)
    assert calls["count"] == 3
    assert not src.exists()
    assert (dst / "file.txt").read_text() == "hi"


def test_replace_directory_falls_back_to_copy_when_rename_never_succeeds(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"; src.mkdir(); (src / "file.txt").write_text("hi")
    dst = tmp_path / "dst"

    def always_locked(self, target):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(Path, "rename", always_locked)
    monkeypatch.setattr("runrail.environments.time.sleep", lambda _: None)
    _replace_directory(src, dst, attempts=3, delay=0)
    assert not src.exists()
    assert (dst / "file.txt").read_text() == "hi"


def test_replace_directory_raises_actionable_error_when_copy_also_fails(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"; src.mkdir(); (src / "file.txt").write_text("hi")
    dst = tmp_path / "dst"

    monkeypatch.setattr(Path, "rename", lambda self, target: (_ for _ in ()).throw(OSError(5, "Access is denied")))
    monkeypatch.setattr("runrail.environments.shutil.copytree",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(5, "Access is denied")))
    monkeypatch.setattr("runrail.environments.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="OneDrive"):
        _replace_directory(src, dst, attempts=2, delay=0)
