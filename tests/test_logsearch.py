"""Bounded log search across runs: scoping, every bound, and the safety rails."""

from datetime import datetime, timedelta, timezone


def make_workflow(client, name):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()


def make_task(client, workflow_id, name):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": "true",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    }).json()


def seed_run(workflow_id, task_id, *, stdout=None, stderr=None, run_status="failed",
             task_status="failed", created_at=None, log_dir=None):
    """A run with real log files on disk, laid out the way the worker lays them out."""
    from runrail.config import get_settings
    from runrail.db import SessionLocal
    from runrail.models import TaskRun, TriggerType, WorkflowRun

    with SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow_id, status=run_status,
                          trigger_type=TriggerType.manual)
        if created_at is not None:
            run.created_at = created_at
        db.add(run); db.flush()
        task_run = TaskRun(workflow_run_id=run.id, task_id=task_id, status=task_status, attempt=1)
        db.add(task_run); db.flush()
        directory = log_dir or get_settings().logs_dir.resolve() / f"run_{run.id}"
        directory.mkdir(parents=True, exist_ok=True)
        for stream, body in (("stdout", stdout), ("stderr", stderr)):
            if body is None:
                continue
            path = directory / f"task_run_{task_run.id}.{stream}.log"
            path.write_bytes(body if isinstance(body, bytes) else body.encode())
            setattr(task_run, f"{stream}_log_path", str(path))
        db.commit()
        return run.id, task_run.id


def search(client, **params):
    response = client.get("/api/logs/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_match_carries_line_number_and_context(client):
    workflow = make_workflow(client, "ingest")
    task = make_task(client, workflow["id"], "fetch_orders")
    run_id, task_run_id = seed_run(
        workflow["id"], task["id"],
        stderr="  File \"fetch.py\", line 55\n    resp = session.get(url)\n"
               "ConnectionResetError: [Errno 104] Connection reset by peer\n"
               "During handling of the above exception\ntrailing\n")

    body = search(client, q="ConnectionResetError")
    assert len(body["matches"]) == 1
    hit = body["matches"][0]
    assert hit["workflow_run_id"] == run_id and hit["task_run_id"] == task_run_id
    assert hit["workflow_name"] == "ingest" and hit["task_name"] == "fetch_orders"
    assert hit["stream"] == "stderr" and hit["line_number"] == 3
    assert hit["line"].startswith("ConnectionResetError")
    assert hit["context_before"] == ["  File \"fetch.py\", line 55", "    resp = session.get(url)"]
    assert hit["context_after"] == ["During handling of the above exception", "trailing"]
    assert body["stats"]["complete"] is True and body["stats"]["stopped_by"] is None
    assert body["stats"]["runs_matched"] == 1 and body["stats"]["files_scanned"] == 1


def test_every_scope_filter_narrows_independently(client):
    alpha = make_workflow(client, "alpha")
    beta = make_workflow(client, "beta")
    fetch = make_task(client, alpha["id"], "fetch")
    load = make_task(client, alpha["id"], "load")
    other = make_task(client, beta["id"], "fetch")

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
    seed_run(alpha["id"], fetch["id"], stderr="boom here\n", created_at=old)
    seed_run(alpha["id"], load["id"], stdout="boom here\n", run_status="success",
             task_status="success")
    seed_run(beta["id"], other["id"], stderr="boom here\n")

    assert len(search(client, q="boom")["matches"]) == 3
    assert {m["workflow_id"] for m in search(client, q="boom", workflow_id=alpha["id"])["matches"]}\
        == {alpha["id"]}
    assert [m["task_id"] for m in search(client, q="boom", task_id=load["id"])["matches"]] \
        == [load["id"]]
    assert {m["task_name"] for m in search(client, q="boom", task_name="load")["matches"]} \
        == {"load"}
    assert {m["stream"] for m in search(client, q="boom", stream="stderr")["matches"]} == {"stderr"}
    assert {m["run_status"] for m in search(client, q="boom", status="success")["matches"]} \
        == {"success"}
    assert {m["task_status"] for m in search(client, q="boom", task_status="failed")["matches"]} \
        == {"failed"}

    # The date range filters on the parent run, not on file mtime.
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert len(search(client, q="boom", since=since)["matches"]) == 2
    assert len(search(client, q="boom", until=since)["matches"]) == 1


def test_case_insensitive_by_default(client):
    workflow = make_workflow(client, "casing")
    task = make_task(client, workflow["id"], "job")
    seed_run(workflow["id"], task["id"], stdout="Timeout waiting for lock\n")

    assert len(search(client, q="timeout")["matches"]) == 1
    assert search(client, q="timeout", case_sensitive=True)["matches"] == []
    assert len(search(client, q="Timeout", case_sensitive=True)["matches"]) == 1


def test_limit_and_max_files_report_the_bound_that_fired(client):
    workflow = make_workflow(client, "noisy")
    task = make_task(client, workflow["id"], "job")
    for _ in range(3):
        seed_run(workflow["id"], task["id"], stdout="hit\nhit\nhit\n")

    capped = search(client, q="hit", limit=4)
    assert len(capped["matches"]) == 4
    assert capped["stats"]["stopped_by"] == "limit" and capped["stats"]["complete"] is False

    narrow = search(client, q="hit", max_files=1)
    assert narrow["stats"]["files_scanned"] == 1
    assert narrow["stats"]["stopped_by"] == "max_files" and narrow["stats"]["complete"] is False

    whole = search(client, q="hit")
    assert len(whole["matches"]) == 9 and whole["stats"]["complete"] is True


def test_time_budget_stops_the_scan(client, monkeypatch):
    """The wall clock is checked between files; a year of logs must not hang."""
    from runrail.db import SessionLocal
    from runrail.logsearch import search_logs

    workflow = make_workflow(client, "slow")
    task = make_task(client, workflow["id"], "job")
    for _ in range(3):
        seed_run(workflow["id"], task["id"], stdout="hit\n")

    # Every reading of the clock jumps a full budget forward, so the deadline
    # is past however many times anything else asks the time first.
    import runrail.logsearch as logsearch
    ticks = iter(range(1000))
    monkeypatch.setattr(logsearch.time, "monotonic", lambda: next(ticks) * 10.0)
    with SessionLocal() as db:
        body = search_logs(db, q="hit", timeout_ms=5000)
    assert body["matches"] == []
    assert body["stats"]["stopped_by"] == "timeout" and body["stats"]["complete"] is False


def test_a_deleted_log_file_is_counted_not_fatal(client):
    from pathlib import Path

    workflow = make_workflow(client, "retained")
    task = make_task(client, workflow["id"], "job")
    kept_run, _ = seed_run(workflow["id"], task["id"], stdout="hit\n")
    _, gone_task_run = seed_run(workflow["id"], task["id"], stdout="hit\n")

    from runrail.db import SessionLocal
    from runrail.models import TaskRun
    with SessionLocal() as db:
        Path(db.get(TaskRun, gone_task_run).stdout_log_path).unlink()

    body = search(client, q="hit")
    assert body["stats"]["files_missing"] == 1
    assert [m["workflow_run_id"] for m in body["matches"]] == [kept_run]


def test_byte_cap_truncates_the_head_but_keeps_line_numbers_exact(client):
    workflow = make_workflow(client, "huge")
    task = make_task(client, workflow["id"], "job")
    lines = [f"line {index} " + "x" * 40 for index in range(200)]
    lines[4] = "needle at line five"
    lines[150] = "needle far past the cap"
    seed_run(workflow["id"], task["id"], stdout="\n".join(lines) + "\n")

    body = search(client, q="needle", max_bytes_per_file=1024)
    assert [m["line_number"] for m in body["matches"]] == [5]
    assert body["stats"]["truncated_files"] == 1
    assert body["stats"]["bytes_scanned"] == 1024
    # The whole file finds both, at the same line numbers.
    assert [m["line_number"] for m in search(client, q="needle")["matches"]] == [5, 151]


def test_regex_is_supported_and_defensive(client):
    workflow = make_workflow(client, "patterns")
    task = make_task(client, workflow["id"], "job")
    seed_run(workflow["id"], task["id"], stdout="exit code 137 killed\n")

    matched = search(client, q=r"exit code \d+", regex=True)
    assert len(matched["matches"]) == 1

    for pattern in ("(unclosed", "(a+)+b", "x" * 201):
        response = client.get("/api/logs/search", params={"q": pattern, "regex": True})
        assert response.status_code == 422, pattern
    # The same strings are harmless as literals.
    assert client.get("/api/logs/search", params={"q": "(a+)+b"}).status_code == 200


def test_a_log_path_outside_the_logs_directory_is_never_read(client, tmp_path):
    """Only DB-sourced paths are opened, but a tampered row must not turn this
    endpoint into an arbitrary-file reader."""
    outside = tmp_path / "elsewhere"
    workflow = make_workflow(client, "tampered")
    task = make_task(client, workflow["id"], "job")
    seed_run(workflow["id"], task["id"], stdout="secret needle\n", log_dir=outside)

    body = search(client, q="secret needle")
    assert body["matches"] == []
    assert body["stats"]["files_scanned"] == 0 and body["stats"]["files_missing"] == 0


def test_ansi_escapes_are_stripped_from_results(client):
    workflow = make_workflow(client, "coloured")
    task = make_task(client, workflow["id"], "job")
    seed_run(workflow["id"], task["id"], stdout="\x1b[31mERROR\x1b[0m: disk full\n")

    hit = search(client, q="ERROR")["matches"][0]
    assert hit["line"] == "ERROR: disk full"


def test_oldest_match_answers_when_it_first_appeared(client):
    workflow = make_workflow(client, "history")
    task = make_task(client, workflow["id"], "job")
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    first, _ = seed_run(workflow["id"], task["id"], stderr="ConnectionReset\n",
                        created_at=base - timedelta(days=30))
    seed_run(workflow["id"], task["id"], stderr="ConnectionReset\n", created_at=base)

    body = search(client, q="ConnectionReset")
    assert body["oldest_match"]["workflow_run_id"] == first
    # Only a complete scan may be reported as a true 'first appeared'.
    assert body["stats"]["complete"] is True
    assert search(client, q="ConnectionReset", limit=1)["stats"]["complete"] is False
    assert search(client, q="nothing here at all")["oldest_match"] is None


def test_a_one_character_query_is_rejected(client):
    assert client.get("/api/logs/search", params={"q": "x"}).status_code == 422
