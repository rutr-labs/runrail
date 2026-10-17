"""The scheduler following edits, and stopped tasks staying stopped.

Each test is a defect found by auditing the schedule and process lifecycle:

- An edited schedule took up to 30 seconds to reach the scheduler, and until
  then the old job still fired: both schedules ran.
- Deleting a workflow mid-run left its task running to the end, unowned.
- A timeout waited for the shell, not its children, so a process that
  handled SIGTERM outlived the kill and overlapped its own retry.
- A force quit (a second Ctrl+C) left every task process running.
- Disabling, auto-pausing or pausing a workflow still ran the iteration already
  waiting in the queue.
- YAML apply wrote workflow settings the API refuses.
"""

import os
import sys
import threading
import time

import pytest
from portable import process_alive, record_pid_and_sleep, then
from test_interactions import make_shell_task, make_workflow, start_run

from runrail.db import SessionLocal
from runrail.models import RunStatus, WorkflowRun
from runrail.scheduler.service import SchedulerService, enqueue_scheduled
from runrail.worker import runners
from runrail.worker.runners import CommandSpec, execute_command, kill_running_tasks
from runrail.worker.service import claim_runnable_run, execute_workflow_run
from runrail.workflow_io import apply_workflows


def _wait_for_pid(pidfile, seconds=15) -> int:
    deadline = time.monotonic() + seconds
    while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    return int(pidfile.read_text())


def _runs(workflow_id):
    with SessionLocal() as db:
        return [(run.trigger_type.value, run.status.value) for run in
                db.query(WorkflowRun).filter(WorkflowRun.workflow_id == workflow_id)]


def test_a_fire_for_a_schedule_the_workflow_no_longer_has_is_dropped(client):
    workflow = make_workflow(client, "edited", schedule_cron="0 9 * * *")
    edited = client.put(f"/api/workflows/{workflow['id']}",
                        json={"name": workflow["name"], "schedule_cron": "1 9 * * *"})
    assert edited.status_code == 200, edited.text
    # The job installed before the edit fires before the next sync replaces it.
    enqueue_scheduled(workflow["id"], "0 9 * * *", "UTC")
    assert _runs(workflow["id"]) == []
    enqueue_scheduled(workflow["id"], "1 9 * * *", "UTC")
    assert _runs(workflow["id"]) == [("schedule", "queued")]


def test_sync_leaves_an_unchanged_job_alone(client, monkeypatch):
    """Replacing a job recomputes its next fire from now, so a sync landing just
    after a fire time, before the scheduler handled it, dropped that fire."""
    make_workflow(client, "steady", schedule_cron="*/5 * * * *")
    service = SchedulerService()
    calls = []
    real_add = service.scheduler.add_job
    monkeypatch.setattr(service.scheduler, "add_job",
                        lambda *a, **k: calls.append(k.get("id")) or real_add(*a, **k))
    service.sync()
    service.sync()
    assert len([c for c in calls if c and c.startswith("workflow-")]) == 1


def test_deleting_a_workflow_mid_run_stops_its_task(client, tmp_path):
    pidfile = tmp_path / "pid"
    workflow = make_workflow(client, "doomed")
    make_shell_task(client, workflow["id"], "slow", record_pid_and_sleep(pidfile, 60))
    start_run(client, workflow["id"])

    def work():
        with SessionLocal() as db:
            try:
                execute_workflow_run(db, claim_runnable_run(db))
            except Exception:
                pass  # its rows are gone; what matters is that it stops

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    pid = _wait_for_pid(pidfile)
    try:
        assert client.delete(f"/api/workflows/{workflow['id']}").status_code == 204
        deadline = time.monotonic() + 10
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not process_alive(pid), "the deleted workflow's task kept running"
        worker.join(timeout=10)
        assert not worker.is_alive()
    finally:
        if process_alive(pid):
            os.kill(pid, 9)


def test_a_timeout_kills_a_child_that_ignores_sigterm(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "TERMINATE_GRACE_SECONDS", 0.5)
    pidfile = tmp_path / "pid"
    stubborn = (f'"{sys.executable}" -c "import os, signal, time; '
                f"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open(r'{pidfile}', 'w').write(str(os.getpid())); time.sleep(60)\"")
    # The shell stays the group leader (no exec), and dies on SIGTERM at once.
    command = then(stubborn, "echo done")
    out, err = tmp_path / "out", tmp_path / "err"
    result = execute_command(CommandSpec(command, True), tmp_path, os.environ.copy(), out, err, 3)
    assert result.exit_code == 124
    pid = int(pidfile.read_text())
    try:
        # Killed, then reaped by init a moment later: until then it is a zombie,
        # which still answers a signal-0 probe.
        deadline = time.monotonic() + 3
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_alive(pid), "a child that ignores SIGTERM outlived the timeout"
    finally:
        if process_alive(pid):
            os.kill(pid, 9)


def test_a_force_quit_takes_running_tasks_with_it(tmp_path):
    pidfile = tmp_path / "pid"
    out, err = tmp_path / "out", tmp_path / "err"
    worker = threading.Thread(target=execute_command, daemon=True, args=(
        CommandSpec(record_pid_and_sleep(pidfile, 60), True), tmp_path, os.environ.copy(),
        out, err, None))
    worker.start()
    pid = _wait_for_pid(pidfile)
    try:
        kill_running_tasks()
        deadline = time.monotonic() + 10
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not process_alive(pid)
        worker.join(timeout=10)
        assert not worker.is_alive()
    finally:
        if process_alive(pid):
            os.kill(pid, 9)


@pytest.mark.parametrize("pause", [
    {"enabled": False},
    {"minutes": 60, "pause_runs": True},
])
def test_pausing_a_workflow_drops_the_iteration_already_queued(client, pause):
    workflow = make_workflow(client, "paused", schedule_cron="* * * * *")
    make_shell_task(client, workflow["id"], "t", "exit 0")
    enqueue_scheduled(workflow["id"])
    manual = start_run(client, workflow["id"])
    if "enabled" in pause:
        response = client.put(f"/api/workflows/{workflow['id']}",
                              json={"name": workflow["name"], **pause})
        assert response.status_code == 200, response.text
    else:
        response = client.post(f"/api/workflows/{workflow['id']}/snooze", params=pause)
        assert response.status_code == 200, response.text

    with SessionLocal() as db:
        claimed = claim_runnable_run(db)
        # The manual run is someone's explicit request, so it still goes ahead.
        assert claimed is not None and claimed.id == manual
    statuses = dict((t, s) for t, s in _runs(workflow["id"]))
    assert statuses["schedule"] == RunStatus.cancelled.value


@pytest.mark.parametrize("bad", [
    {"max_concurrent_runs": 0},
    {"schedule_timezone": "Mars/Olympus_Mons", "schedule_cron": "0 9 * * *"},
    {"sla_minutes": -5},
])
def test_yaml_apply_refuses_what_the_api_refuses(client, bad):
    with SessionLocal() as db, pytest.raises(ValueError):
        apply_workflows(db, {"workflows": [{"name": "from-yaml", **bad, "tasks": [
            {"name": "t", "task_type": "shell", "command": "exit 0"}]}]})


def test_a_run_uses_the_task_definitions_it_started_with(client, tmp_path):
    """Editing a task mid-run used to change its command for this run while the
    run kept the old dependency graph. Edits apply from the next run."""
    from portable import emit, wait_for
    flag = tmp_path / "go"
    workflow = make_workflow(client, "edited-mid-run")
    make_shell_task(client, workflow["id"], "first", wait_for(flag, seconds=20))
    second = make_shell_task(client, workflow["id"], "second", emit("as-started"),
                             depends_on=["first"])
    run_id = start_run(client, workflow["id"])

    def work():
        with SessionLocal() as db:
            execute_workflow_run(db, claim_runnable_run(db))

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not client.get(f"/api/runs/{run_id}").json()["task_runs"]:
        time.sleep(0.05)

    edited = client.put(f"/api/tasks/{second['id']}", json={
        "name": "second", "task_type": "shell", "command": emit("edited"),
        "depends_on_json": ["first"], "retries": 0, "retry_delay_seconds": 0})
    assert edited.status_code == 200, edited.text
    # Deleting is refused while the run is executing: it will still reach the task.
    assert client.delete(f"/api/tasks/{second['id']}").status_code == 409
    flag.touch()
    worker.join(timeout=30)

    rows = client.get(f"/api/runs/{run_id}").json()["task_runs"]
    second_row = next(row for row in rows if row["task_name"] == "second")
    assert client.get(f"/api/task-runs/{second_row['id']}/stdout").text == "as-started"
    # The edit is what the next run gets, and the task can go now.
    assert client.delete(f"/api/tasks/{second['id']}").status_code == 204
