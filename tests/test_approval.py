"""Manual approval gate: the run parks on a human, holding no worker slot."""

from pathlib import Path


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


def execute_next_run(client):
    """One turn of the worker loop, through the same claim path it uses."""
    from runrail.db import SessionLocal
    from runrail.worker.service import claim_runnable_run, execute_workflow_run
    with SessionLocal() as db:
        run = claim_runnable_run(db)
        if run is None:
            return None
        execute_workflow_run(db, run)
        return run.id


def gates(client, run_id=None):
    return [gate for gate in client.get("/api/approvals").json()
            if run_id is None or gate["run_id"] == run_id]


def test_a_gate_parks_the_run_and_frees_the_worker_slot(client):
    """The global slot is released, so an unrelated workflow runs to completion
    while the gate waits. If the executor blocked instead, this test hangs."""
    gated = make_workflow(client, "needs-a-human")
    make_shell_task(client, gated["id"], "publish", "printf published",
                    requires_approval=True, approval_prompt="Row counts look right?")
    other = make_workflow(client, "unrelated")
    make_shell_task(client, other["id"], "job", "printf ok")
    client.post(f"/api/workflows/{gated['id']}/run", json={"parameters": {}})
    client.post(f"/api/workflows/{other['id']}/run", json={"parameters": {}})

    parked = execute_next_run(client)
    detail = client.get(f"/api/runs/{parked}").json()
    assert detail["status"] == "waiting_approval"
    assert detail["finished_at"] is None  # the finished_at fallback must not fire
    assert [(t["task_name"], t["status"], t["attempt"]) for t in detail["task_runs"]] == [
        ("publish", "awaiting_approval", 0)]

    unrelated = execute_next_run(client)
    assert unrelated is not None and unrelated != parked
    assert client.get(f"/api/runs/{unrelated}").json()["status"] == "success"

    open_gate = gates(client, parked)
    assert len(open_gate) == 1
    assert open_gate[0]["workflow_name"] == "needs-a-human"
    assert open_gate[0]["task_name"] == "publish"
    assert open_gate[0]["prompt"] == "Row counts look right?"


def test_a_parallel_branch_finishes_while_the_gate_waits(client, tmp_path: Path):
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "two-branches")
    make_shell_task(client, workflow["id"], "gated", f'printf g >> "{ran}"',
                    requires_approval=True)
    make_shell_task(client, workflow["id"], "free", f'printf f >> "{ran}"')
    make_shell_task(client, workflow["id"], "after-free", f'printf a >> "{ran}"',
                    depends_on=["free"])
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "waiting_approval"
    assert {t["task_name"]: t["status"] for t in detail["task_runs"]} == {
        "gated": "awaiting_approval", "free": "success", "after-free": "success"}
    assert ran.read_text() == "fa"  # the gated task has not run


def test_approving_continues_the_run_to_success(client, tmp_path: Path):
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "approved")
    make_shell_task(client, workflow["id"], "publish", f'printf p >> "{ran}"',
                    requires_approval=True)
    make_shell_task(client, workflow["id"], "announce", f'printf a >> "{ran}"',
                    depends_on=["publish"])
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)
    gate_id = gates(client, run_id)[0]["id"]

    decided = client.post(f"/api/task-runs/{gate_id}/approve",
                          json={"note": "counts check out"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    assert decided.json()["approval_note"] == "counts check out"
    assert decided.json()["approved_at"] is not None
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"

    assert execute_next_run(client) == run_id
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "success"
    assert ran.read_text() == "pa"
    # The gate stays a separate row and never lands success — otherwise the
    # resume reuse check would read it as the task itself.
    rows = sorted((t["task_name"], t["attempt"], t["status"]) for t in detail["task_runs"])
    assert rows == [("announce", 1, "success"), ("publish", 0, "approved"),
                    ("publish", 1, "success")]


def test_rejecting_skips_downstream_and_cancels_the_run(client, tmp_path: Path, monkeypatch):
    """A rejection is a decision, not a failure: no run_failed alert and no
    auto-pause, even with the threshold at one."""
    sent = []
    monkeypatch.setattr("runrail.notify._post",
                        lambda url, text, **fields: sent.append(fields))
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "rejected", auto_pause_failures=1,
                             notify_webhook_url="https://hooks.example/x")
    make_shell_task(client, workflow["id"], "publish", f'printf p >> "{ran}"',
                    requires_approval=True)
    make_shell_task(client, workflow["id"], "announce", f'printf a >> "{ran}"',
                    depends_on=["publish"])
    make_shell_task(client, workflow["id"], "sibling", f'printf s >> "{ran}"')
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)
    gate_id = gates(client, run_id)[0]["id"]

    rejected = client.post(f"/api/task-runs/{gate_id}/reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert execute_next_run(client) == run_id

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "cancelled"
    assert detail["finished_at"] is not None
    assert {t["task_name"]: t["status"] for t in detail["task_runs"]} == {
        "publish": "rejected", "announce": "skipped", "sibling": "success"}
    assert ran.read_text() == "s"
    assert [event["event"] for event in sent] == ["approval_requested", "approval_rejected"]
    assert client.get(f"/api/workflows/{workflow['id']}").json()["enabled"] is True


def test_deciding_a_gate_twice_is_a_409(client):
    workflow = make_workflow(client, "double-click")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)
    gate_id = gates(client, run_id)[0]["id"]

    assert client.post(f"/api/task-runs/{gate_id}/approve").status_code == 200
    second = client.post(f"/api/task-runs/{gate_id}/approve")
    assert second.status_code == 409 and "already approved" in second.json()["detail"]
    assert client.post(f"/api/task-runs/{gate_id}/reject").status_code == 409
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"


def test_two_parallel_gates_re_enter_only_once_both_are_decided(client, tmp_path: Path):
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "two-gates")
    make_shell_task(client, workflow["id"], "left", f'printf l >> "{ran}"', requires_approval=True)
    make_shell_task(client, workflow["id"], "right", f'printf r >> "{ran}"', requires_approval=True)
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)
    open_gates = {gate["task_name"]: gate["id"] for gate in gates(client, run_id)}
    assert set(open_gates) == {"left", "right"}

    client.post(f"/api/task-runs/{open_gates['left']}/approve")
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "waiting_approval"
    assert execute_next_run(client) is None  # nothing claimable: the run still waits

    client.post(f"/api/task-runs/{open_gates['right']}/reject")
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"
    assert execute_next_run(client) == run_id
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "cancelled"  # one rejection, no failure
    assert ran.read_text() == "l"


def test_a_waiting_run_holds_its_workflows_concurrency_slot(client):
    """Otherwise a scheduled workflow launches its next run straight past the
    same gate while a human is still deciding on the first."""
    workflow = make_workflow(client, "serial", max_concurrent_runs=1)
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    first = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()
    second = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    assert execute_next_run(client) == first["id"]
    assert execute_next_run(client) is None  # the second run is not claimable
    assert client.get(f"/api/runs/{second['id']}").json()["status"] == "queued"

    gate_id = gates(client, first["id"])[0]["id"]
    client.post(f"/api/task-runs/{gate_id}/approve")
    assert execute_next_run(client) == first["id"]
    assert client.get(f"/api/runs/{first['id']}").json()["status"] == "success"
    assert execute_next_run(client) == second["id"]  # the slot is free again


def test_a_waiting_run_survives_worker_restart_recovery(client):
    from runrail.db import SessionLocal
    from runrail.worker.service import recover_interrupted_runs

    workflow = make_workflow(client, "restarted")
    make_shell_task(client, workflow["id"], "publish", "printf p", requires_approval=True)
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)

    with SessionLocal() as db:
        assert recover_interrupted_runs(db) == 0
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "waiting_approval"
    assert detail["task_runs"][0]["status"] == "awaiting_approval"


def test_resuming_a_rejected_run_asks_again(client, tmp_path: Path):
    """The 'actually, go ahead' path: a rejection is not a permanent answer."""
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "second-thoughts")
    make_shell_task(client, workflow["id"], "publish", f'printf p >> "{ran}"',
                    requires_approval=True)
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)
    client.post(f"/api/task-runs/{gates(client, run_id)[0]['id']}/reject",
                json={"note": "wrong week"})
    execute_next_run(client)
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "cancelled"

    resumed = client.post(f"/api/runs/{run_id}/resume")
    assert resumed.status_code == 200 and resumed.json()["resume_count"] == 1
    assert execute_next_run(client) == run_id
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "waiting_approval"

    reopened = gates(client, run_id)
    assert len(reopened) == 1 and reopened[0]["resume_index"] == 1
    client.post(f"/api/task-runs/{reopened[0]['id']}/approve")
    assert execute_next_run(client) == run_id
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "success"
    assert ran.read_text() == "p"


def test_an_approved_task_that_succeeded_is_reused_by_a_later_resume(client, tmp_path: Path):
    """A resume must not re-ask for a gate whose work already landed — that is
    the whole point of reusing the successful upstream."""
    ran = tmp_path / "ran"
    fixed = tmp_path / "fixed"
    workflow = make_workflow(client, "approved-then-broken")
    make_shell_task(client, workflow["id"], "publish", f'printf p >> "{ran}"',
                    requires_approval=True)
    make_shell_task(client, workflow["id"], "verify", f'test -f "{fixed}"',
                    depends_on=["publish"])
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_next_run(client)
    client.post(f"/api/task-runs/{gates(client, run_id)[0]['id']}/approve")
    execute_next_run(client)
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "failed"

    fixed.touch()
    client.post(f"/api/runs/{run_id}/resume")
    assert execute_next_run(client) == run_id
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "success"
    assert gates(client, run_id) == []   # never asked twice
    assert ran.read_text() == "p"        # and the publish did not repeat


def test_editing_a_task_does_not_silently_remove_its_gate(client):
    """A form that forgets a field must not reset it.

    The task modal sends the fields it knows about; when it did not know about
    approval, saving an unrelated change turned a task that waits for a person
    into one that runs unattended, with no warning and no trace. Omitted now
    means unchanged, so only an explicit `false` can remove a gate.
    """
    workflow = make_workflow(client, "editable")
    task = make_shell_task(client, workflow["id"], "publish", "printf p",
                           requires_approval=True)
    assert task["requires_approval"] is True

    # Exactly the body the edit modal sent before this was fixed: every field
    # it knew about, and nothing about approval.
    unrelated_edit = client.put(f"/api/tasks/{task['id']}", json={
        "name": "publish", "task_type": "shell", "command": "printf p",
        "cwd": None, "depends_on_json": [], "parameters_json": None,
        "retries": 2, "retry_delay_seconds": 0, "timeout_seconds": None,
        "project_id": None, "environment_id": None,
    })
    assert unrelated_edit.status_code == 200
    kept = unrelated_edit.json()
    assert kept["requires_approval"] is True, "an unrelated edit removed the gate"
    assert kept["retries"] == 2, "the edit itself must still apply"

    # Removing a gate stays possible — it just has to be asked for.
    removed = client.put(f"/api/tasks/{task['id']}", json={
        "name": "publish", "task_type": "shell", "command": "printf p",
        "requires_approval": False, "approval_prompt": None,
    })
    assert removed.status_code == 200
    assert removed.json()["requires_approval"] is False
