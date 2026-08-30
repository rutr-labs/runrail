"""Where the recent features overlap: the atomic claim in worker/queue.py and
the run lifecycle in worker/service.py.

Resume, approval gates, resource locks, backfill, crash recovery and the
watchdogs are each unit-tested alone, and every one of them mutates those same
two things. Only the pairings are tested here, in that order: resume x locks,
approval x locks, approval x concurrency and the schedule, resume x approval,
locks x backfill, crash recovery x everything, and the watchdogs against the
two run states the batch added.
"""

from datetime import date, timedelta

from runrail.db import SessionLocal
from runrail.models import RunStatus, TaskRun, TaskRunStatus, Workflow, WorkflowRun, now
from runrail.worker.queue import claim_next_run
from runrail.worker.service import (
    claim_runnable_run,
    execute_workflow_run,
    recover_interrupted_runs,
)

HOOK = "https://hooks.example/x"


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1, **extra,
    }).json()


def make_shell_task(client, workflow_id, name, command, depends_on=None, **extra):
    response = client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": depends_on or [], "retries": 0, "retry_delay_seconds": 0, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def start_run(client, workflow_id) -> int:
    return client.post(f"/api/workflows/{workflow_id}/run", json={"parameters": {}}).json()["id"]


def backfill(client, workflow_id, start: date, end: date) -> list[int]:
    response = client.post(f"/api/workflows/{workflow_id}/backfill",
                           json={"from": start.isoformat(), "to": end.isoformat()})
    assert response.status_code == 201, response.text
    return [run["id"] for run in response.json()]


def execute_next_run(client):
    """One turn of the worker loop, through the claim path the worker uses."""
    with SessionLocal() as db:
        run = claim_runnable_run(db)
        if run is None:
            return None
        execute_workflow_run(db, run)
        return run.id


def drain(client, turns: int = 20) -> list[int]:
    """Run the worker loop until nothing is claimable; the ids, in claim order."""
    executed = []
    for _ in range(turns):
        run_id = execute_next_run(client)
        if run_id is None:
            return executed
        executed.append(run_id)
    raise AssertionError("worker loop did not settle")


def claim_only(client):
    """Claim without executing — the row state a worker holds mid-run."""
    with SessionLocal() as db:
        run = claim_next_run(db)
        return run.id if run else None


def gates(client, run_id=None):
    return [gate for gate in client.get("/api/approvals").json()
            if run_id is None or gate["run_id"] == run_id]


def run_status(client, run_id) -> str:
    return client.get(f"/api/runs/{run_id}").json()["status"]


def set_status(run_id: int, status: RunStatus) -> None:
    with SessionLocal() as db:
        db.get(WorkflowRun, run_id).status = status
        db.commit()


def age_run(run_id: int, **delta) -> None:
    """Runs are aged by writing created_at, never by sleeping."""
    with SessionLocal() as db:
        db.get(WorkflowRun, run_id).created_at = now() - timedelta(**delta)
        db.commit()


def capture_webhooks(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr("runrail.notify._post",
                        lambda url, text, **fields: sent.append(fields))
    return sent


# Resume x locks.
def test_a_resumed_run_retakes_its_lock_and_holds_it_while_it_re_executes(client, tmp_path):
    """A resume goes back through claim_next_run, so the resumed segment acquires
    the resource exactly like a first execution — and bars the neighbour again."""
    fixed = tmp_path / "fixed"
    heavy = make_workflow(client, "monthly-maintenance",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", f'test -f "{fixed}"')
    hourly = make_workflow(client, "hourly-load",
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "printf ok")

    heavy_run = start_run(client, heavy["id"])
    assert execute_next_run(client) == heavy_run
    assert run_status(client, heavy_run) == "failed"

    hourly_run = start_run(client, hourly["id"])
    fixed.touch()
    assert client.post(f"/api/runs/{heavy_run}/resume").status_code == 200
    # The failure released the warehouse, so the neighbour is claimable right up
    # to the moment the resume re-enters the queue ahead of it.
    assert claim_only(client) == heavy_run
    assert claim_only(client) is None      # re-acquired: the neighbour waits again
    assert run_status(client, hourly_run) == "queued"

    set_status(heavy_run, RunStatus.success)
    assert execute_next_run(client) == hourly_run
    assert run_status(client, hourly_run) == "success"


def test_a_resume_does_not_jump_the_starvation_barrier(client):
    """A resumed shared run is a NEW claim, so a queued exclusive run bars it the
    same as any other — the resume carries no priority from its first segment."""
    hourly = make_workflow(client, "hourly-load", max_concurrent_runs=2,
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "exit 1")
    heavy = make_workflow(client, "monthly-maintenance",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", "printf ok")

    hourly_run = start_run(client, hourly["id"])
    assert execute_next_run(client) == hourly_run
    assert run_status(client, hourly_run) == "failed"

    heavy_run = start_run(client, heavy["id"])
    client.post(f"/api/runs/{hourly_run}/resume")
    # Both are queued; the exclusive one is younger but the barrier is what orders
    # them, not created_at.
    assert execute_next_run(client) == heavy_run
    assert run_status(client, hourly_run) == "queued"
    assert execute_next_run(client) == hourly_run


# Approval x locks.
def test_cancelling_a_run_parked_inside_a_lock_releases_the_resource(client):
    """Approve and reject are a parked run's only other exits, so cancel is the
    operator's way out of a gate nobody will decide — and it must free the
    resource, not just the run."""
    heavy = make_workflow(client, "monthly-maintenance",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", "printf v", requires_approval=True)
    hourly = make_workflow(client, "hourly-load",
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "printf ok")

    parked = start_run(client, heavy["id"])
    assert execute_next_run(client) == parked
    hourly_run = start_run(client, hourly["id"])
    assert run_status(client, parked) == "waiting_approval"
    assert execute_next_run(client) is None      # the parked run still holds it

    assert client.post(f"/api/runs/{parked}/cancel").status_code == 200
    # The gate goes with the run: a cancelled run must not leave a decision open.
    assert gates(client, parked) == []
    with SessionLocal() as db:
        statuses = [row.status for row in db.query(TaskRun).filter(
            TaskRun.workflow_run_id == parked)]
    assert statuses == [TaskRunStatus.cancelled]
    assert execute_next_run(client) == hourly_run


def test_approving_inside_a_lock_finishes_the_run_and_frees_the_resource(client, tmp_path):
    ran = tmp_path / "ran"
    heavy = make_workflow(client, "monthly-maintenance",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", f'printf v >> "{ran}"',
                    requires_approval=True)
    make_shell_task(client, heavy["id"], "analyze", f'printf a >> "{ran}"',
                    depends_on=["vacuum"])
    hourly = make_workflow(client, "hourly-load",
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "printf ok")

    parked = start_run(client, heavy["id"])
    assert execute_next_run(client) == parked
    hourly_run = start_run(client, hourly["id"])

    client.post(f"/api/task-runs/{gates(client, parked)[0]['id']}/approve")
    assert drain(client) == [parked, hourly_run]
    assert run_status(client, parked) == "success"
    assert ran.read_text() == "va"
    assert run_status(client, hourly_run) == "success"


def test_an_approved_run_keeps_its_lock_until_it_is_re_claimed(client, tmp_path):
    ran = tmp_path / "ran"
    hourly = make_workflow(client, "hourly-load",
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "stage", f'printf s >> "{ran}"')
    make_shell_task(client, hourly["id"], "load", f'printf l >> "{ran}"',
                    depends_on=["stage"], requires_approval=True)
    heavy = make_workflow(client, "monthly-maintenance",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", "printf v")

    parked = start_run(client, hourly["id"])
    assert execute_next_run(client) == parked
    heavy_run = start_run(client, heavy["id"])
    assert execute_next_run(client) is None      # correct: the gate holds the lock
    assert ran.read_text() == "s"                # half-written, against the warehouse

    client.post(f"/api/task-runs/{gates(client, parked)[0]['id']}/approve")
    # That half-written state is still there and the run resumes into the rest of
    # the graph, so nothing else may take the warehouse in between.
    assert claim_only(client) == parked
    assert run_status(client, heavy_run) == "queued"


# Approval x concurrency and the schedule.
def test_a_parked_run_coalesces_the_schedule_to_one_queued_iteration(client):
    """A gate can wait for days. enqueue_scheduled must keep exactly one
    iteration behind it — not one per cron fire — and the worker must not start
    that iteration underneath the run holding the slot."""
    from runrail.scheduler.service import enqueue_scheduled

    workflow = make_workflow(client, "gated-nightly", schedule_cron="0 2 * * *")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    parked = start_run(client, workflow["id"])
    assert execute_next_run(client) == parked
    assert run_status(client, parked) == "waiting_approval"

    for _ in range(3):
        enqueue_scheduled(workflow["id"])
    queued = [run for run in client.get("/api/runs",
                                        params={"workflow_id": workflow["id"]}).json()
              if run["status"] == "queued"]
    assert len(queued) == 1
    assert execute_next_run(client) is None      # the parked run holds the only slot

    client.post(f"/api/task-runs/{gates(client, parked)[0]['id']}/approve")
    # The approved run is older, so it finishes before the iteration behind it.
    assert drain(client) == [parked, queued[0]["id"]]
    assert run_status(client, parked) == "success"


def test_the_raw_claim_is_what_withholds_the_slot_from_a_parked_runs_sibling(client):
    """queue.py's _occupies_slot already counts waiting_approval, so the sibling
    is refused one layer below claim_runnable_run.

    Pinned apart from the coalescing test because it is the claim that enforces
    this: service.py's _over_gate_budget re-check is only a backstop for two
    claimers racing the same budget.
    """
    workflow = make_workflow(client, "serial-gate")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    parked = start_run(client, workflow["id"])
    sibling = start_run(client, workflow["id"])
    assert execute_next_run(client) == parked

    assert claim_only(client) is None
    assert run_status(client, sibling) == "queued"


def test_a_backfill_of_a_gated_workflow_opens_one_gate_at_a_time(client, tmp_path):
    """The pile-up the slot accounting exists to prevent: without it a 30-day
    backfill of a gated workflow would open 30 approvals at once and let them
    execute out of order."""
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "gated-backfill")
    make_shell_task(client, workflow["id"], "publish", f'printf p >> "{ran}"',
                    requires_approval=True)
    days = backfill(client, workflow["id"], date(2026, 1, 1), date(2026, 1, 4))

    for _ in days:
        assert execute_next_run(client) is not None      # claims, parks on its gate
        open_now = gates(client)
        assert len(open_now) == 1
        client.post(f"/api/task-runs/{open_now[0]['id']}/approve")
        assert execute_next_run(client) is not None      # re-enters and finishes
    assert {run_status(client, run_id) for run_id in days} == {"success"}
    assert ran.read_text() == "p" * len(days)


def test_a_rejected_run_frees_the_slot_for_the_coalesced_iteration(client, tmp_path):
    """A rejection lands the run cancelled, which releases the slot: the schedule
    picks up where it left off rather than staying wedged behind the decision."""
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "gated-nightly")
    make_shell_task(client, workflow["id"], "publish", f'printf p >> "{ran}"',
                    requires_approval=True)
    parked = start_run(client, workflow["id"])
    waiting = start_run(client, workflow["id"])
    assert execute_next_run(client) == parked
    assert execute_next_run(client) is None

    client.post(f"/api/task-runs/{gates(client, parked)[0]['id']}/reject")
    assert execute_next_run(client) == parked
    assert run_status(client, parked) == "cancelled"
    # The second iteration is not a repeat of the decision: it opens its own gate.
    assert execute_next_run(client) == waiting
    assert run_status(client, waiting) == "waiting_approval"
    assert [gate["run_id"] for gate in gates(client)] == [waiting]
    assert not ran.exists()


# Resume x approval.
def test_resuming_after_the_approved_task_itself_failed_asks_again(client, tmp_path):
    """The complement of the reuse case: the approval authorised work that never
    landed, so the new segment must not inherit it. _gate_decision is scoped to
    resume_index, which is what makes this true."""
    fixed = tmp_path / "fixed"
    workflow = make_workflow(client, "authorised-but-broken")
    make_shell_task(client, workflow["id"], "publish", f'test -f "{fixed}"',
                    requires_approval=True)
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    first_gate = gates(client, run_id)[0]["id"]
    client.post(f"/api/task-runs/{first_gate}/approve")
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "failed"

    fixed.touch()
    client.post(f"/api/runs/{run_id}/resume")
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "waiting_approval"
    reopened = gates(client, run_id)
    assert len(reopened) == 1
    assert reopened[0]["resume_index"] == 1 and reopened[0]["id"] != first_gate

    client.post(f"/api/task-runs/{reopened[0]['id']}/approve")
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "success"


def test_forcing_a_rerun_of_an_approved_task_re_opens_its_gate(client, tmp_path):
    """Unticking a reused gated task has to re-ask: the resume writes a decided
    non-success row for it, which puts the task back in the walk's re-run set and
    leaves _gate_decision with no answer for the new segment."""
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "re-authorise")
    make_shell_task(client, workflow["id"], "publish", f'printf p >> "{ran}"',
                    requires_approval=True)
    make_shell_task(client, workflow["id"], "verify", "exit 1", depends_on=["publish"])
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    client.post(f"/api/task-runs/{gates(client, run_id)[0]['id']}/approve")
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "failed"

    plan = client.get(f"/api/runs/{run_id}/resume-plan", params={"rerun": ["publish"]}).json()
    assert {item["task"]: item["reason"] for item in plan["rerun"]} == {
        "publish": "you chose to", "verify": "failed"}

    client.post(f"/api/runs/{run_id}/resume", json={"rerun": ["publish"]})
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "waiting_approval"
    assert [gate["resume_index"] for gate in gates(client, run_id)] == [1]
    assert ran.read_text() == "p"    # still only the first, authorised execution


# Locks x backfill.
def test_a_backfill_against_an_exclusive_resource_drains_one_at_a_time(client):
    """The lock is on the resource, not the workflow, so a backfill of an
    exclusive workflow serialises against itself even with concurrency to spare —
    and it drains rather than deadlocking."""
    heavy = make_workflow(client, "rebuild-marts", max_concurrent_runs=4,
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "rebuild", "printf r")
    queued = backfill(client, heavy["id"], date(2026, 1, 1), date(2026, 1, 6))
    assert len(queued) == 6

    held = claim_only(client)
    assert held in queued
    assert claim_only(client) is None          # one holder, exclusive, no overlap
    set_status(held, RunStatus.queued)         # hand it back and let the range drain
    assert sorted(drain(client)) == sorted(queued)
    assert {run_status(client, run_id) for run_id in queued} == {"success"}


def test_a_shared_backfill_overlaps_up_to_its_concurrency_limit(client):
    """Shared is the other half of the same claim: the resource permits overlap
    and max_concurrent_runs is then the only budget left."""
    reports = make_workflow(client, "daily-report", max_concurrent_runs=2,
                            lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, reports["id"], "render", "printf r")
    queued = backfill(client, reports["id"], date(2026, 1, 1), date(2026, 1, 5))

    with SessionLocal() as db:
        claimed = [claim_next_run(db), claim_next_run(db), claim_next_run(db)]
    assert [run is not None for run in claimed] == [True, True, False]
    assert {run.id for run in claimed[:2]} <= set(queued)


def test_an_exclusive_backfill_starves_a_shared_neighbour_for_its_whole_range(client):
    """The starvation barrier is symmetric, and this is the cost of it: a queued
    exclusive run bars every NEW shared run, and a backfill keeps one queued for
    the whole range. The hourly load does not run again until the backfill ends.

    Documented, not asserted as desirable — an operator backfilling 30 days of an
    exclusive workflow silently stops every shared workflow on that resource."""
    heavy = make_workflow(client, "rebuild-marts",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "rebuild", "printf r")
    hourly = make_workflow(client, "hourly-load",
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "printf ok")

    days = backfill(client, heavy["id"], date(2026, 1, 1), date(2026, 1, 4))
    hourly_run = start_run(client, hourly["id"])

    executed = drain(client)
    assert sorted(executed[:len(days)]) == sorted(days)
    assert executed[-1] == hourly_run          # last, not interleaved
    assert run_status(client, hourly_run) == "success"


# Crash recovery x everything.
def test_recovery_across_running_parked_and_queued_runs_frees_only_the_dead_ones(client):
    """The mixed fleet a kill actually leaves: one run mid-execution holding an
    exclusive resource, one parked on a human, one queued behind each. Recovery
    must end exactly the first kind."""
    heavy = make_workflow(client, "monthly-maintenance",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", "printf v")
    gated = make_workflow(client, "needs-a-human")
    make_shell_task(client, gated["id"], "publish", "printf p", requires_approval=True)
    hourly = make_workflow(client, "hourly-load",
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "printf ok")

    parked = start_run(client, gated["id"])
    assert execute_next_run(client) == parked
    killed = start_run(client, heavy["id"])
    assert claim_only(client) == killed        # claimed, never executed: the kill
    blocked = start_run(client, hourly["id"])
    waiting = start_run(client, gated["id"])

    with SessionLocal() as db:
        assert recover_interrupted_runs(db) == 1     # only the running one
    assert run_status(client, killed) == "failed"
    assert run_status(client, parked) == "waiting_approval"
    assert run_status(client, waiting) == "queued"

    # The dead run's warehouse lock went with it; the gate's slot did not.
    assert execute_next_run(client) == blocked
    assert execute_next_run(client) is None
    assert run_status(client, waiting) == "queued"


def test_recovery_leaves_a_parked_run_holding_its_lock_and_a_cancel_frees_it(client):
    """Deliberate: nobody has decided, and the run resumes into the same tasks,
    so the resource stays held across the restart. Verified end to end because
    it is the one hold that survives a process death."""
    heavy = make_workflow(client, "monthly-maintenance",
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", "printf v", requires_approval=True)
    hourly = make_workflow(client, "hourly-load",
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "printf ok")

    parked = start_run(client, heavy["id"])
    assert execute_next_run(client) == parked
    blocked = start_run(client, hourly["id"])

    with SessionLocal() as db:
        assert recover_interrupted_runs(db) == 0
    assert execute_next_run(client) is None
    assert gates(client, parked)                # still decidable after the restart

    client.post(f"/api/runs/{parked}/cancel")
    assert execute_next_run(client) == blocked


def test_a_crash_inside_a_resumed_segment_keeps_the_earlier_reuse(client, tmp_path):
    """Recovery and resume compose: a segment killed mid-flight is just another
    failure, and the successes banked before it survive into segment two."""
    ran = tmp_path / "ran"
    fixed = tmp_path / "fixed"
    workflow = make_workflow(client, "chain")
    make_shell_task(client, workflow["id"], "extract", f'printf e >> "{ran}"')
    make_shell_task(client, workflow["id"], "load", f'printf l >> "{ran}"; test -f "{fixed}"',
                    depends_on=["extract"])
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "failed"

    client.post(f"/api/runs/{run_id}/resume")
    assert claim_only(client) == run_id            # claimed, then the worker dies
    with SessionLocal() as db:
        assert recover_interrupted_runs(db) == 1
    assert run_status(client, run_id) == "failed"

    plan = client.get(f"/api/runs/{run_id}/resume-plan").json()
    assert [item["task"] for item in plan["reuse"]] == ["extract"]

    fixed.touch()
    resumed = client.post(f"/api/runs/{run_id}/resume")
    assert resumed.status_code == 200 and resumed.json()["resume_count"] == 2
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "success"
    # The killed segment executed nothing, so 'load' ran twice and 'extract' once.
    assert ran.read_text() == "ell"


def strand_a_gate(run_id: int) -> None:
    """The row state a kill leaves when one branch had opened a gate while another
    was still executing: the run is `running`, the gate row is `awaiting_approval`.
    Recovery is what settles that row — the gate dies with the segment."""
    set_status(run_id, RunStatus.running)
    with SessionLocal() as db:
        assert recover_interrupted_runs(db) == 1


def test_a_gate_stranded_by_a_crash_does_not_wedge_the_resumed_run(client):
    workflow = make_workflow(client, "crashed-mid-gate")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    make_shell_task(client, workflow["id"], "sibling", "printf s")
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    strand_a_gate(run_id)
    assert run_status(client, run_id) == "failed"

    client.post(f"/api/runs/{run_id}/resume")
    assert execute_next_run(client) == run_id
    current = [gate for gate in gates(client, run_id) if gate["resume_index"] == 1]
    assert len(current) == 1
    client.post(f"/api/task-runs/{current[0]['id']}/approve")

    assert run_status(client, run_id) == "queued"
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "success"


def test_a_gate_on_a_crashed_run_is_settled_and_not_offered(client):
    """Both halves: /api/approvals filters on the run's status, so the dead
    segment's gate is invisible, and recovery settles the row itself — an open
    row would outlive its segment and wedge every later one."""
    workflow = make_workflow(client, "crashed-mid-gate")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    strand_a_gate(run_id)

    assert gates(client, run_id) == []
    with SessionLocal() as db:
        stale = db.query(TaskRun).filter(TaskRun.workflow_run_id == run_id).one()
    assert stale.status == TaskRunStatus.cancelled
    assert stale.error_message == "Interrupted by worker shutdown"


def test_cancelling_a_running_run_settles_the_gate_a_branch_left_open(client):
    """The other way a gate outlives its run: cancel arrives while one branch is
    still executing, so the run finalizes through the worker and nothing else
    would ever close the gate row."""
    workflow = make_workflow(client, "cancelled-mid-gate")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    set_status(run_id, RunStatus.running)      # the sibling branch is still going

    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    with SessionLocal() as db:
        gate = db.query(TaskRun).filter(TaskRun.workflow_run_id == run_id).one()
    assert gate.status == TaskRunStatus.cancelled
    # A running run finalizes through its worker, so the cancel settles the row
    # without stamping a finish time on the run.
    assert client.get(f"/api/runs/{run_id}").json()["finished_at"] is None


def test_a_cancel_that_races_the_gate_open_leaves_no_row_behind(client, monkeypatch):
    """The interleaving cancel_run cannot settle: the executor read the
    cancellation flag before the request landed and writes the gate row after it.
    The run finalizes terminal, so the finalize is the last chance to close it."""
    from runrail.worker import service

    workflow = make_workflow(client, "cancel-races-the-gate")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    run_id = start_run(client, workflow["id"])
    opened = service.open_gate

    def cancel_first(db, run, task):
        assert client.post(f"/api/runs/{run.id}/cancel").status_code == 200
        return opened(db, run, task)

    monkeypatch.setattr(service, "open_gate", cancel_first)
    assert execute_next_run(client) == run_id

    assert run_status(client, run_id) == "cancelled"
    with SessionLocal() as db:
        gate = db.query(TaskRun).filter(TaskRun.workflow_run_id == run_id).one()
    assert gate.status == TaskRunStatus.cancelled


def test_a_gate_left_open_by_a_dead_segment_does_not_count_against_this_one(client):
    """The scoping on its own, without recovery to settle the row first: a
    database written before gates were settled still carries open rows from dead
    segments, and re-entry must ignore them exactly as _gate_decision does."""
    workflow = make_workflow(client, "stale-gate")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    set_status(run_id, RunStatus.failed)       # the crash, minus the settling

    client.post(f"/api/runs/{run_id}/resume")
    assert execute_next_run(client) == run_id
    current = [gate for gate in gates(client, run_id) if gate["resume_index"] == 1]
    assert len(current) == 1
    client.post(f"/api/task-runs/{current[0]['id']}/approve")

    assert run_status(client, run_id) == "queued"
    assert execute_next_run(client) == run_id
    assert run_status(client, run_id) == "success"


# Watchdogs x the new run states.
def run_watchdogs() -> None:
    from runrail.scheduler.service import check_watchdogs
    with SessionLocal() as db:
        check_watchdogs(db)


def age_workflow(workflow_id: int, **delta) -> None:
    """updated_at anchors the missed-run check, so it is always written explicitly."""
    with SessionLocal() as db:
        db.get(Workflow, workflow_id).updated_at = now() - timedelta(**delta)
        db.commit()


def test_a_parked_run_does_not_trip_the_missed_run_watchdog(client, monkeypatch):
    """_IN_FLIGHT includes waiting_approval: a workflow waiting on a human is
    busy, not silent, and a dead man's switch that fired here would cry wolf on
    every gate left open overnight."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "gated-nightly", notify_webhook_url=HOOK,
                             schedule_cron="0 2 * * *", missed_run_grace_minutes=30)
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    run_id = start_run(client, workflow["id"])
    assert execute_next_run(client) == run_id
    sent.clear()

    # Three days, so the 02:00 fire the watchdog expects is in the past whatever
    # time of day the suite runs at.
    age_run(run_id, days=3)
    age_workflow(workflow["id"], days=3)
    run_watchdogs()
    assert [event.get("event") for event in sent] == []
    with SessionLocal() as db:
        assert db.get(Workflow, workflow["id"]).missed_notified_at is None

    # The same three days with nothing in flight is what the watchdog is for.
    client.post(f"/api/runs/{run_id}/cancel")
    run_watchdogs()
    assert [event.get("event") for event in sent] == ["run_missed"]


def test_a_run_parked_far_past_its_sla_is_the_one_reported(client, monkeypatch):
    """The two watchdogs read the same _IN_FLIGHT set and say different things
    about it: the missed-run switch stays silent because the workflow is busy,
    and the SLA watchdog reports it anyway — busy is not healthy, and the
    operator who set sla_minutes asked about exactly this run."""
    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "gated-nightly", notify_webhook_url=HOOK,
                             schedule_cron="0 2 * * *", missed_run_grace_minutes=30,
                             sla_minutes=30)
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    parked = start_run(client, workflow["id"])
    assert execute_next_run(client) == parked
    sent.clear()

    age_run(parked, hours=8)
    age_workflow(workflow["id"], hours=8)
    run_watchdogs()
    assert [event.get("event") for event in sent] == ["sla_breached"]
    with SessionLocal() as db:
        assert db.get(WorkflowRun, parked).sla_breached_at is not None

    # Marked once: a gate left open overnight does not re-alert every pass.
    sent.clear()
    run_watchdogs()
    assert [event.get("event") for event in sent] == []


def test_the_iteration_queued_behind_a_gate_is_not_the_one_blamed(client, monkeypatch):
    """The coalesced iteration is queued and old enough to breach on its own
    clock, but it is late BECAUSE of the gate ahead of it. Only the oldest
    in-flight run of a workflow breaches, so the alert names the gate."""
    from runrail.scheduler.service import enqueue_scheduled

    sent = capture_webhooks(monkeypatch)
    workflow = make_workflow(client, "gated-nightly", notify_webhook_url=HOOK,
                             schedule_cron="0 2 * * *", sla_minutes=30)
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    parked = start_run(client, workflow["id"])
    assert execute_next_run(client) == parked
    enqueue_scheduled(workflow["id"])
    waiting = [run["id"] for run in client.get(
        "/api/runs", params={"workflow_id": workflow["id"]}).json()
        if run["status"] == "queued"]
    assert len(waiting) == 1
    sent.clear()

    age_run(parked, hours=8)
    age_run(waiting[0], hours=4)
    run_watchdogs()
    assert [event.get("event") for event in sent] == ["sla_breached"]
    with SessionLocal() as db:
        assert db.get(WorkflowRun, parked).sla_breached_at is not None
        assert db.get(WorkflowRun, waiting[0]).sla_breached_at is None


def test_a_lock_blocked_run_breaches_its_sla_while_the_holder_is_silent(client, monkeypatch):
    """A resource lock is invisible to both watchdogs — the blocked run is queued,
    so the SLA watchdog reports it as late with no hint that another workflow is
    the reason. Pinned because it is the honest limit of the feature."""
    sent = capture_webhooks(monkeypatch)
    heavy = make_workflow(client, "monthly-maintenance", notify_webhook_url=HOOK,
                          lock_resource="warehouse", lock_mode="exclusive")
    make_shell_task(client, heavy["id"], "vacuum", "printf v")
    hourly = make_workflow(client, "hourly-load", notify_webhook_url=HOOK, sla_minutes=15,
                           lock_resource="warehouse", lock_mode="shared")
    make_shell_task(client, hourly["id"], "load", "printf ok")

    holder = start_run(client, heavy["id"])
    assert claim_only(client) == holder
    blocked = start_run(client, hourly["id"])
    age_run(blocked, hours=2)
    run_watchdogs()

    assert [event.get("event") for event in sent] == ["sla_breached"]
    with SessionLocal() as db:
        assert db.get(WorkflowRun, blocked).sla_breached_at is not None
