"""Parallel DAG execution: independent tasks run concurrently, dependents wait."""

from pathlib import Path


def make_workflow(client, name):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()


def make_shell_task(client, workflow_id, name, command, depends_on=None):
    response = client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": depends_on or [], "retries": 0, "retry_delay_seconds": 0,
    })
    assert response.status_code == 201, response.text
    return response.json()


def execute_queued_run(client):
    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run
    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None
        execute_workflow_run(db, run)
        return run.id


def test_independent_tasks_run_in_parallel_and_dependents_wait(client, tmp_path: Path):
    """The t1/t2/t3 scenario: t3 depends on t1 only. t1 and t2 must start
    together (t1 blocks until t2 creates a flag file — impossible if execution
    were sequential, since t1 sorts first), and t3 starts only after t1 ends."""
    flag = tmp_path / "t2-started.flag"
    workflow = make_workflow(client, "parallel")
    make_shell_task(client, workflow["id"], "t1",
                    f'for i in $(seq 1 100); do [ -f "{flag}" ] && exit 0; sleep 0.1; done; exit 1')
    make_shell_task(client, workflow["id"], "t2", f'touch "{flag}"')
    make_shell_task(client, workflow["id"], "t3", "printf t3-done", depends_on=["t1"])
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "success"
    by_name = {t["task_name"]: t for t in detail["task_runs"]}
    assert {name: t["status"] for name, t in by_name.items()} == {
        "t1": "success", "t2": "success", "t3": "success",
    }
    # Dependency ordering: t3 must not start before t1 finished.
    assert by_name["t3"]["started_at"] >= by_name["t1"]["finished_at"]


def test_failed_dependency_skips_downstream_but_not_siblings(client):
    workflow = make_workflow(client, "skip-branch")
    make_shell_task(client, workflow["id"], "t1", "exit 1")
    make_shell_task(client, workflow["id"], "t2", "printf ok")
    make_shell_task(client, workflow["id"], "t3", "printf never", depends_on=["t1"])
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "failed"
    statuses = {t["task_name"]: t["status"] for t in detail["task_runs"]}
    assert statuses == {"t1": "failed", "t2": "success", "t3": "skipped"}


def test_task_parameters_reach_the_rendered_command(client, tmp_path: Path):
    """parameters_json set on a task must be rendered into its template."""
    out = tmp_path / "param.txt"
    workflow = make_workflow(client, "task-params")
    response = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "emit", "task_type": "shell",
        "command": f'printf "%s" "{{{{ region }}}}" > "{out}"',
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
        "parameters_json": {"region": "ca"},
    })
    assert response.status_code == 201, response.text
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)

    assert client.get(f"/api/runs/{run_id}").json()["status"] == "success"
    assert out.read_text() == "ca"
