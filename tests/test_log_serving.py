"""Every endpoint that opens a file named by a database row: what it refuses,
what it bounds, and how much of a file it reads.

Found by auditing log search, log serving, artifacts and the shared export:
one pathological regex could stall the whole server for hours, a request could
ask for an unbounded read, an imported history searched as "complete, nothing
found", only log search checked that a path was inside the home, finished logs
were read whole in the platform's default encoding, and exports overshot the
size they promised.
"""

import time

import pytest
from test_logsearch import make_task, make_workflow, search, seed_run

from runrail import logsearch
from runrail.api import routes_ws
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.models import Artifact, ArtifactType, TaskRun


def test_the_time_budget_is_checked_inside_a_file(client, monkeypatch):
    """The deadline used to be checked between files only, so one slow pattern
    on one big file ran on for as long as it took."""
    workflow = make_workflow(client, "w")
    task = make_task(client, workflow["id"], "t")
    seed_run(workflow["id"], task["id"], stdout=b"x" * 50 + b"\n" + (b"line\n" * 5000))
    real = time.monotonic
    clock = {"now": real()}

    def ticking():  # every read of the clock is another 10 ms, far past a 100 ms budget
        clock["now"] += 0.01
        return clock["now"]

    monkeypatch.setattr(logsearch.time, "monotonic", ticking)
    result = search(client, q="never", timeout_ms=100)
    assert result["stats"]["stopped_by"] == "timeout" and result["stats"]["complete"] is False


def test_a_pattern_is_matched_against_a_bounded_slice_of_a_line(client):
    workflow = make_workflow(client, "w")
    task = make_task(client, workflow["id"], "t")
    seed_run(workflow["id"], task["id"], stdout=b"a" * (logsearch.MAX_MATCH_BYTES + 10) + b" needle\n")
    assert search(client, q="needle")["matches"] == [], "matched beyond the cap"
    assert search(client, q="aaaa")["matches"]


def test_total_bytes_read_are_capped(client, monkeypatch):
    workflow = make_workflow(client, "w")
    task = make_task(client, workflow["id"], "t")
    for _ in range(3):
        seed_run(workflow["id"], task["id"], stdout=b"needle " + b"x" * 1000 + b"\n")
    monkeypatch.setattr(logsearch, "MAX_TOTAL_BYTES", 1500)
    result = search(client, q="needle")
    assert result["stats"]["stopped_by"] == "max_bytes"
    assert result["stats"]["bytes_scanned"] <= 1500
    assert client.get("/api/logs/search", params={"q": "x", "max_bytes_per_file": 10**12}).status_code == 422


def test_files_outside_the_logs_directory_are_counted_not_hidden(client, tmp_path):
    """An imported history whose rows still named the old home searched as
    "complete scan, nothing found"."""
    workflow = make_workflow(client, "w")
    task = make_task(client, workflow["id"], "t")
    elsewhere = tmp_path / "elsewhere"
    seed_run(workflow["id"], task["id"], stdout=b"secret needle\n", log_dir=elsewhere)
    result = search(client, q="needle")
    assert result["matches"] == []
    assert result["stats"]["files_out_of_scope"] == 1 and result["stats"]["complete"] is False


def test_case_insensitive_search_folds_non_ascii_letters(client):
    workflow = make_workflow(client, "w")
    task = make_task(client, workflow["id"], "t")
    seed_run(workflow["id"], task["id"], stdout="Ärger beim Laden\n".encode())
    assert len(search(client, q="ärger")["matches"]) == 1
    assert len(search(client, q="ÄRGER .* laden", regex="true")["matches"]) == 1


def test_log_and_artifact_endpoints_refuse_files_outside_the_home(client, tmp_path):
    """A row pointing elsewhere — hand-edited, or imported — must not turn the
    run page into a reader of arbitrary files, nor the export into a mailer of them."""
    workflow = make_workflow(client, "w")
    task = make_task(client, workflow["id"], "t")
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    run_id, task_run_id = seed_run(workflow["id"], task["id"], stdout=b"ok\n")
    with SessionLocal() as db:
        db.get(TaskRun, task_run_id).stderr_log_path = str(secret)
        db.add(Artifact(task_run_id=task_run_id, workflow_run_id=run_id, name="a.txt",
                        artifact_type=ArtifactType.other, path=str(secret)))
        db.commit()
        artifact_id = db.query(Artifact).one().id
    assert client.get(f"/api/task-runs/{task_run_id}/stdout").text == "ok\n"
    assert client.get(f"/api/task-runs/{task_run_id}/stderr").status_code == 404
    assert client.get(f"/api/artifacts/{artifact_id}/download").status_code == 404
    export = client.get(f"/api/runs/{run_id}/export")
    assert "PRIVATE KEY" not in export.text


def test_a_finished_log_is_served_as_utf8_and_streamed(client):
    workflow = make_workflow(client, "w")
    task = make_task(client, workflow["id"], "t")
    _, task_run_id = seed_run(workflow["id"], task["id"], stdout="naïve café\n".encode())
    response = client.get(f"/api/task-runs/{task_run_id}/stdout")
    assert response.text == "naïve café\n"
    assert response.headers["content-type"].startswith("text/plain")
    assert "charset=utf-8" in response.headers["content-type"]


def test_the_live_tail_starts_near_the_end_and_follows_a_truncated_file(tmp_path, monkeypatch):
    monkeypatch.setattr(routes_ws, "FIRST_READ_BYTES", 10)
    monkeypatch.setattr(routes_ws, "CHUNK_BYTES", 8)
    log = tmp_path / "x.log"
    log.write_bytes(b"0123456789abcdefghij")
    chunk, offset = routes_ws._read_from(log, None)
    assert chunk == b"abcdefgh" and offset == 18          # the tail, one chunk at a time
    chunk, offset = routes_ws._read_from(log, offset)
    assert chunk == b"ij" and offset == 20
    log.write_bytes(b"new")                              # truncated and rewritten
    chunk, offset = routes_ws._read_from(log, offset)
    assert chunk == b"new" and offset == 3


@pytest.mark.parametrize("path", ["\\\\\\\\evil\\\\share", "//evil/share"])
def test_the_file_browser_refuses_network_paths_before_touching_them(client, path):
    assert client.get("/api/filesystem", params={"path": path}).status_code == 403


def test_a_services_log_tail_reaches_back_through_rotated_files(client, tmp_path):
    from runrail.services.supervisor import log_tail
    directory = get_settings().logs_dir.resolve() / "services" / "9"
    directory.mkdir(parents=True)
    current = directory / "session_1.log"
    (directory / "session_1.log.1").write_bytes(b"older line\n" * 100)
    current.write_bytes(b"just rotated\n")
    text = log_tail(str(current), 60)
    assert text.endswith("just rotated\n") and "older line" in text
    assert log_tail(str(tmp_path / "outside.log"), 60) == ""


def test_deleting_a_service_removes_its_logs(client):
    service = client.post("/api/services", json={"name": "s", "command": "x"}).json()
    directory = get_settings().logs_dir.resolve() / "services" / str(service["id"])
    directory.mkdir(parents=True)
    (directory / "session_1.log").write_text("hello")
    assert client.delete(f"/api/services/{service['id']}").status_code == 204
    assert not directory.exists()
