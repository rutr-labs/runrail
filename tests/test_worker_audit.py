"""The worker, audited: each test here is a way a run ended in the wrong state,
ran a task twice, or stopped the worker for good. Real subprocesses throughout;
the races are forced with threads and files rather than hoped for."""

import threading
import time
from datetime import timedelta

import pytest
from portable import WINDOWS, append, emit, pause, wait_for
from sqlalchemy.exc import OperationalError
from test_interactions import execute_next_run, make_shell_task, make_workflow, start_run

from runrail.db import SessionLocal
from runrail.models import RunStatus, TaskRun, WorkflowRun, now
from runrail.worker import service as worker
from runrail.worker.service import (
    claim_runnable_run,
    execute_workflow_run,
    recover_interrupted_runs,
)


def run_json(client, run_id) -> dict:
    return client.get(f"/api/runs/{run_id}").json()


def in_thread(target) -> tuple[threading.Thread, dict]:
    out: dict = {}
    thread = threading.Thread(target=lambda: out.setdefault("value", target()), daemon=True)
    thread.start()
    return thread, out


def wait_until(check, seconds=15, what="the condition"):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def test_a_gate_decided_while_another_branch_runs_still_re_enters(client, tmp_path):
    """The decision found the run `running`, so it could not requeue it; the
    executor then parked it on a gate nobody could decide again. The run held
    its slot and lock until someone cancelled it."""
    release = tmp_path / "release"
    workflow = make_workflow(client, "race")
    make_shell_task(client, workflow["id"], "gated", emit("g"), requires_approval=True)
    make_shell_task(client, workflow["id"], "slow", wait_for(release, seconds=30))
    run_id = start_run(client, workflow["id"])
    thread, _ = in_thread(lambda: execute_next_run(client))
    wait_until(lambda: client.get("/api/approvals").json(), what="the gate")
    gate = client.get("/api/approvals").json()[0]
    assert client.post(f"/api/task-runs/{gate['id']}/approve").status_code == 200
    release.touch()
    thread.join(30)
    assert run_json(client, run_id)["status"] == "queued", "parked on a gate already decided"
    execute_next_run(client)
    assert run_json(client, run_id)["status"] == "success"


def test_another_worker_starting_up_leaves_live_runs_alone(client, tmp_path):
    """Recovery failed every `running` run, whoever was executing it: starting
    a second worker failed the first one's runs, freed their slots, and the
    next run of the same workflow ran alongside the one still going."""
    release, trace = tmp_path / "release", tmp_path / "trace"
    workflow = make_workflow(client, "live")
    make_shell_task(client, workflow["id"], "a", wait_for(release, seconds=30) + " && " + append("a", trace))
    run_id = start_run(client, workflow["id"])
    second = start_run(client, workflow["id"])
    alive = worker.WorkerHeartbeat("worker-a")
    alive.beat()
    with SessionLocal() as db:
        run = claim_runnable_run(db, owner="worker-a")
    thread, _ = in_thread(lambda: _execute(run.id))
    wait_until(lambda: run_json(client, run_id)["status"] == "running", what="the run to start")
    with SessionLocal() as db:
        assert recover_interrupted_runs(db, owner="worker-b") == 0
        assert claim_runnable_run(db, owner="worker-b") is None, "a second run of the workflow was let in"
    release.touch()
    thread.join(30)
    assert run_json(client, run_id)["status"] == "success"
    assert run_json(client, second)["status"] == "queued"


def test_a_run_whose_worker_stopped_beating_is_recovered(client):
    """…and one whose worker really died is still recovered, by any worker."""
    workflow = make_workflow(client, "dead")
    make_shell_task(client, workflow["id"], "a", emit("a"))
    run_id = start_run(client, workflow["id"])
    gone = worker.WorkerHeartbeat("worker-gone")
    gone.beat(at=now() - worker.WORKER_STALE_AFTER - timedelta(seconds=5))
    with SessionLocal() as db:
        claimed = claim_runnable_run(db, owner="worker-gone")
        assert claimed.id == run_id
        assert recover_interrupted_runs(db, owner="worker-b") == 1
    assert run_json(client, run_id)["status"] == "failed"


def _execute(run_id: int) -> None:
    with SessionLocal() as db:
        execute_workflow_run(db, db.get(WorkflowRun, run_id))


def test_a_cancelled_run_cannot_be_resumed_until_its_worker_has_let_go(client, tmp_path):
    """Resume accepted a run whose cancel had not landed yet; a worker claimed
    the new segment while the old executor, waking from its retry delay, saw
    `running` and started attempt 2 of the old segment beside it."""
    trace = tmp_path / "trace"
    workflow = make_workflow(client, "rd")
    make_shell_task(client, workflow["id"], "t", append("x", trace) + " && exit 1",
                    retries=1, retry_delay_seconds=3)
    run_id = start_run(client, workflow["id"])
    thread, _ = in_thread(lambda: execute_next_run(client))
    wait_until(lambda: trace.exists(), what="attempt 1")
    time.sleep(0.5)  # attempt 1 has failed; the executor sleeps the retry delay
    assert client.post(f"/api/runs/{run_id}/cancel").json()["status"] == "cancelled"
    refused = client.post(f"/api/runs/{run_id}/resume")
    assert refused.status_code == 409 and "still stopping" in refused.json()["detail"]
    thread.join(30)
    assert run_json(client, run_id)["finished_at"], "the cancel never landed"
    # The retry delay was cut short by the cancel, not slept through.
    assert trace.read_text() == "x", "attempt 2 started after the cancel"
    assert client.post(f"/api/runs/{run_id}/resume").status_code == 200


def test_the_retry_delay_ends_when_the_run_is_cancelled(client, tmp_path):
    trace = tmp_path / "trace"
    workflow = make_workflow(client, "delay")
    make_shell_task(client, workflow["id"], "t", append("x", trace) + " && exit 1",
                    retries=1, retry_delay_seconds=60)
    run_id = start_run(client, workflow["id"])
    thread, _ = in_thread(lambda: execute_next_run(client))
    wait_until(lambda: trace.exists(), what="attempt 1")
    time.sleep(0.5)
    started = time.monotonic()
    client.post(f"/api/runs/{run_id}/cancel")
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 5, "slept through the retry delay"


def test_one_database_hiccup_does_not_stop_the_worker(client, monkeypatch):
    """The claim loop had no guard: one 'database is locked' and the worker
    thread was gone, while `serve` kept answering as if nothing had happened."""
    calls = {"n": 0}
    real = worker.claim_next_environment

    def flaky(db):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return real(db)

    monkeypatch.setattr(worker, "claim_next_environment", flaky)
    monkeypatch.setattr(worker, "CLAIM_ERROR_BACKOFF_SECONDS", 0.1)
    service = worker.WorkerService(concurrency=1)
    monkeypatch.setattr(service.supervisor, "run", lambda: None)
    workflow = make_workflow(client, "after-hiccup")
    make_shell_task(client, workflow["id"], "t", emit("ok"))
    thread = threading.Thread(target=service.run, kwargs={"install_signals": False}, daemon=True)
    thread.start()
    try:
        wait_until(lambda: calls["n"] >= 3, what="the loop to carry on")
        assert thread.is_alive()
        run_id = start_run(client, workflow["id"])
        wait_until(lambda: run_json(client, run_id)["status"] == "success", what="the run")
    finally:
        service.stop()
        thread.join(15)


def test_a_cancel_stops_tasks_still_waiting_for_a_thread(client, tmp_path, monkeypatch):
    """Every ready task was handed to the pool at once; the ones waiting for a
    thread started after the cancel, each living a second before being killed —
    long enough for a deploy or a POST to complete in full."""
    from runrail import runtime_settings
    real = runtime_settings.current
    monkeypatch.setattr(runtime_settings, "current", lambda key: 1 if key == "task_parallelism" else real(key))
    trace = tmp_path / "trace"
    workflow = make_workflow(client, "queued-in-pool")
    for name in "abcd":
        make_shell_task(client, workflow["id"], name, append(name, trace) + " && " + pause(2))
    run_id = start_run(client, workflow["id"])
    thread, _ = in_thread(lambda: execute_next_run(client))
    wait_until(lambda: trace.exists(), what="the first task")
    client.post(f"/api/runs/{run_id}/cancel")
    thread.join(30)
    assert len(trace.read_text()) == 1, f"tasks started after the cancel: {trace.read_text()}"
    rows = run_json(client, run_id)["task_runs"]
    assert sorted(r["status"] for r in rows) == ["cancelled"] * 4


def test_re_entry_after_a_gate_does_not_re_run_what_already_failed(client, tmp_path):
    """Gate re-entry reused resume's reuse set, which re-runs failures: a
    failed task ran again on approval, doubling its retry budget per gate."""
    trace = tmp_path / "trace"
    workflow = make_workflow(client, "rerun")
    make_shell_task(client, workflow["id"], "gated", append("g", trace), requires_approval=True)
    make_shell_task(client, workflow["id"], "flaky", append("f", trace) + " && exit 1")
    make_shell_task(client, workflow["id"], "after", append("a", trace), depends_on=["flaky"])
    run_id = start_run(client, workflow["id"])
    execute_next_run(client)
    assert run_json(client, run_id)["status"] == "waiting_approval"
    gate = client.get("/api/approvals").json()[0]
    client.post(f"/api/task-runs/{gate['id']}/approve")
    execute_next_run(client)
    run = run_json(client, run_id)
    assert sorted(trace.read_text()) == ["f", "g"], f"re-ran: {trace.read_text()}"
    assert run["status"] == "failed"
    assert [r["status"] for r in run["task_runs"] if r["task_name"] == "after"] == ["skipped"]


@pytest.mark.skipif(WINDOWS, reason="the template is rendered the same everywhere")
def test_a_template_error_in_a_path_fails_the_task_row_it_created(client):
    environment = client.post("/api/environments", json={
        "name": "sys", "env_type": "python", "executable": __import__("sys").executable}).json()
    workflow = make_workflow(client, "render", default_environment_id=environment["id"])
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "py", "task_type": "python", "script_path": "{{ nope }}/x.py",
        "retries": 0, "retry_delay_seconds": 0})
    run_id = start_run(client, workflow["id"])
    execute_next_run(client)
    rows = run_json(client, run_id)["task_runs"]
    assert [(r["status"], bool(r["finished_at"])) for r in rows] == [("failed", True)], rows
    assert "nope" in rows[0]["error_message"]


def test_a_cancel_that_lands_mid_claim_still_gets_a_finish_time(client):
    """Cancelled between the claim and the requeue, a run never had its finish
    time stamped — and retention selects on finished_at, so it was kept forever."""
    workflow = make_workflow(client, "mid-claim")
    make_shell_task(client, workflow["id"], "t", emit("t"))
    run_id = start_run(client, workflow["id"])
    with SessionLocal() as db:
        from runrail.worker.queue import claim_next_run
        run = claim_next_run(db)
        client.post(f"/api/runs/{run_id}/cancel")
        worker._requeue(db, run)
    landed = run_json(client, run_id)
    assert landed["status"] == "cancelled" and landed["finished_at"]


def test_runs_waiting_on_a_build_do_not_hold_up_everything_behind_them(client, monkeypatch):
    """The claim pass skipped run by run, fifty at most: a backfill of fifty
    runs waiting on one environment build hid every other workflow's runs."""
    monkeypatch.setattr(worker, "_CLAIM_SKIP_LIMIT", 3)
    blocked = make_workflow(client, "backfilled", max_concurrent_runs=10)
    make_shell_task(client, blocked["id"], "t", emit("t"))
    other = make_workflow(client, "other")
    make_shell_task(client, other["id"], "t", emit("t"))
    for _ in range(5):
        start_run(client, blocked["id"])
    wanted = start_run(client, other["id"])
    monkeypatch.setattr(worker, "_building_environment",
                        lambda db, run: object() if run.workflow_id == blocked["id"] else None)
    with SessionLocal() as db:
        claimed = claim_runnable_run(db)
    assert claimed is not None and claimed.id == wanted


def test_run_parameters_cannot_overwrite_what_runrail_provides(client):
    workflow = make_workflow(client, "reserved")
    make_shell_task(client, workflow["id"], "t", emit("{{ task_run_id }} {{ run_id }}"))
    run_id = client.post(f"/api/workflows/{workflow['id']}/run",
                         json={"parameters": {"task_run_id": "x", "run_id": "y"}}).json()["id"]
    execute_next_run(client)
    with SessionLocal() as db:
        row = db.query(TaskRun).filter(TaskRun.workflow_run_id == run_id).one()
        assert row.rendered_command and f"{row.id} {run_id}" in row.rendered_command
        assert db.get(WorkflowRun, run_id).status == RunStatus.success
