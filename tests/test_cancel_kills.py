"""Cancelling a run has to stop the work, not just relabel it.

Before this, cancel only marked the run cancelled. The worker sat in
process.wait() until the command finished on its own, so a long task kept
running — for hours, with no timeout set — under a run the UI already showed as
cancelled. Then the retry loop would start the next attempt.

The process is tracked by the PID it reports for itself. Matching a marker in
`ps` output does not work: `sh -c` strips comments before exec'ing, so the real
process never carries the marker, and the only match is the shell that launched
the test — which is how a first attempt at this test "proved" the bug and the
fix alike.
"""

import os
import threading
import time

from test_interactions import make_shell_task, make_workflow, start_run

from runrail.db import SessionLocal
from runrail.worker.service import claim_runnable_run, execute_workflow_run


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cancelling_a_run_kills_the_running_task_and_skips_its_retries(client, tmp_path):
    pidfile = tmp_path / "task.pid"
    workflow = make_workflow(client, "long-task")
    # exec keeps the PID, so the recorded one IS the sleeping process.
    make_shell_task(client, workflow["id"], "slow", f"echo $$ > {pidfile}; exec sleep 60",
                    retries=2)
    run_id = start_run(client, workflow["id"])

    def work():
        with SessionLocal() as db:
            execute_workflow_run(db, claim_runnable_run(db))

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    assert _alive(pid)

    try:
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
        deadline = time.monotonic() + 8
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(pid), "the task's process outlived the cancellation"

        worker.join(timeout=8)
        assert not worker.is_alive(), "the worker went on to a retry instead of stopping"
        rows = client.get(f"/api/runs/{run_id}").json()["task_runs"]
        assert [(row["attempt"], row["status"]) for row in rows] == [(1, "cancelled")]
    finally:
        if _alive(pid):
            os.kill(pid, 9)
