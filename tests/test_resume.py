"""Resume: reopen the same run and re-execute only what did not succeed."""

from datetime import datetime, timedelta, timezone
from pathlib import Path


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1, **extra,
    }).json()


def task_body(name, command, depends_on=None, **extra):
    return {"name": name, "task_type": "shell", "command": command,
            "depends_on_json": depends_on or [], "retries": 0, "retry_delay_seconds": 0, **extra}


def make_shell_task(client, workflow_id, name, command, depends_on=None, **extra):
    response = client.post(f"/api/workflows/{workflow_id}/tasks",
                           json=task_body(name, command, depends_on, **extra))
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


def run_now(client, workflow_id):
    client.post(f"/api/workflows/{workflow_id}/run", json={"parameters": {}})
    return execute_queued_run(client)


def test_resume_reuses_successful_upstream(client, tmp_path: Path):
    """a -> b -> c with b failing: a keeps its one TaskRun, b and c re-execute
    in a new segment, and the run id never changes."""
    ran = tmp_path / "ran"
    fixed = tmp_path / "fixed"
    workflow = make_workflow(client, "chain")
    make_shell_task(client, workflow["id"], "a", f'printf a >> "{ran}"')
    make_shell_task(client, workflow["id"], "b", f'printf b >> "{ran}"; test -f "{fixed}"',
                    depends_on=["a"])
    make_shell_task(client, workflow["id"], "c", f'printf c >> "{ran}"', depends_on=["b"])
    run_id = run_now(client, workflow["id"])
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "failed"

    fixed.touch()
    response = client.post(f"/api/runs/{run_id}/resume")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == run_id       # the SAME run, not a new one
    assert response.json()["resume_count"] == 1
    assert execute_queued_run(client) == run_id

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "success"
    assert ran.read_text() == "abbc"             # 'a' never ran a second time
    segments = {(t["task_name"], t["resume_index"], t["status"]) for t in detail["task_runs"]}
    assert segments == {("a", 0, "success"), ("b", 0, "failed"), ("c", 0, "skipped"),
                        ("b", 1, "success"), ("c", 1, "success")}


def test_resume_of_a_mid_dag_failure_keeps_the_parallel_branch(client, tmp_path: Path):
    """Diamond a -> (b, c) -> d with c failing: b's success survives the resume
    even though it sits on the branch that did not break."""
    ran = tmp_path / "ran"
    fixed = tmp_path / "fixed"
    workflow = make_workflow(client, "diamond")
    make_shell_task(client, workflow["id"], "a", f'printf a >> "{ran}"')
    make_shell_task(client, workflow["id"], "b", f'printf b >> "{ran}"', depends_on=["a"])
    make_shell_task(client, workflow["id"], "c", f'printf c >> "{ran}"; test -f "{fixed}"',
                    depends_on=["a"])
    make_shell_task(client, workflow["id"], "d", f'printf d >> "{ran}"', depends_on=["b", "c"])
    run_id = run_now(client, workflow["id"])
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "failed"

    plan = client.get(f"/api/runs/{run_id}/resume-plan").json()
    assert plan["resumable"] is True
    assert sorted(item["task"] for item in plan["reuse"]) == ["a", "b"]
    assert {item["task"]: item["reason"] for item in plan["rerun"]} == {
        "c": "failed", "d": "upstream re-running"}

    fixed.touch()
    client.post(f"/api/runs/{run_id}/resume")
    assert execute_queued_run(client) == run_id

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "success"
    # b and c race in the first segment, so compare multisets: a, b, c once each
    # plus c and d again. Neither a nor b ran twice.
    assert sorted(ran.read_text()) == list("abccd")
    second = {t["task_name"] for t in detail["task_runs"] if t["resume_index"] == 1}
    assert second == {"c", "d"}


def test_resume_after_the_workflow_changed(client, tmp_path: Path):
    """The reuse set is computed against the CURRENT definition: a task that
    gained an upstream it never ran with cannot be reused."""
    ran = tmp_path / "ran"
    workflow = make_workflow(client, "edited")
    a = make_shell_task(client, workflow["id"], "a", f'printf a >> "{ran}"')
    make_shell_task(client, workflow["id"], "b", "exit 1", depends_on=["a"])
    run_id = run_now(client, workflow["id"])
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "failed"

    make_shell_task(client, workflow["id"], "guard", f'printf g >> "{ran}"')
    response = client.put(f"/api/tasks/{a['id']}",
                          json=task_body("a", f'printf a >> "{ran}"', ["guard"]))
    assert response.status_code == 200, response.text

    plan = client.get(f"/api/runs/{run_id}/resume-plan").json()
    assert plan["reuse"] == []
    assert {item["task"]: item["reason"] for item in plan["rerun"]} == {
        "guard": "did not run", "a": "upstream re-running", "b": "failed"}

    client.post(f"/api/runs/{run_id}/resume")
    assert execute_queued_run(client) == run_id
    assert ran.read_text() == "aga"  # the new upstream, then 'a' again
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "failed"


def test_resume_keeps_the_run_date_and_artifacts_directory(client, tmp_path: Path):
    """The property that rules out a new-run model: a resumed segment renders
    the same ds and writes to the same artifacts directory."""
    from runrail.db import SessionLocal
    from runrail.models import WorkflowRun

    stamps = tmp_path / "stamps"
    fixed = tmp_path / "fixed"
    workflow = make_workflow(client, "dated")
    make_shell_task(client, workflow["id"], "gate-keeper",
                    f'printf "{{{{ ds }}}} {{{{ artifacts_dir }}}}\n" >> "{stamps}"; '
                    f'test -f "{fixed}"')
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run = client.get("/api/runs").json()[0]
    yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    with SessionLocal() as db:
        db.query(WorkflowRun).filter(WorkflowRun.id == run["id"]).update({"created_at": yesterday})
        db.commit()
    execute_queued_run(client)

    fixed.touch()
    client.post(f"/api/runs/{run['id']}/resume")
    execute_queued_run(client)

    first, second = stamps.read_text().splitlines()
    assert first == second
    assert first.startswith(yesterday.strftime("%Y-%m-%d"))  # not today
    assert first.endswith(str(run["id"]))


def test_forcing_a_rerun_discards_a_reusable_task(client, tmp_path: Path):
    """Unticking a reuse row must survive the round trip to the worker, which
    recomputes the plan and never sees the request body."""
    ran = tmp_path / "ran"
    fixed = tmp_path / "fixed"
    workflow = make_workflow(client, "forced")
    make_shell_task(client, workflow["id"], "a", f'printf a >> "{ran}"')
    make_shell_task(client, workflow["id"], "b", f'printf b >> "{ran}"; test -f "{fixed}"',
                    depends_on=["a"])
    run_id = run_now(client, workflow["id"])

    plan = client.get(f"/api/runs/{run_id}/resume-plan", params={"rerun": ["a"]}).json()
    assert [item["task"] for item in plan["reuse"]] == []
    assert {item["task"]: item["reason"] for item in plan["rerun"]} == {
        "a": "you chose to", "b": "failed"}

    fixed.touch()
    client.post(f"/api/runs/{run_id}/resume", json={"rerun": ["a"]})
    assert execute_queued_run(client) == run_id
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "success"
    assert ran.read_text() == "abab"


def test_resume_refuses_runs_that_are_live_successful_or_already_resumed(client):
    workflow = make_workflow(client, "guards")
    make_shell_task(client, workflow["id"], "ok", "printf ok")
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    queued = client.post(f"/api/runs/{run['id']}/resume")
    assert queued.status_code == 409 and "queued" in queued.json()["detail"]
    assert client.get(f"/api/runs/{run['id']}/resume-plan").json()["resumable"] is False

    execute_queued_run(client)
    assert client.post(f"/api/runs/{run['id']}/resume").status_code == 409  # success

    make_shell_task(client, workflow["id"], "boom", "exit 1")
    failed = run_now(client, workflow["id"])
    assert client.post(f"/api/runs/{failed}/resume").status_code == 200
    # Second click of the same button: the guarded update finds no failed run.
    assert client.post(f"/api/runs/{failed}/resume").status_code == 409
    assert client.get(f"/api/runs/{failed}").json()["resume_count"] == 1


def test_a_resumed_run_stays_one_row_in_the_stats(client):
    """Reopening keeps one logical execution as one run: the failure is
    replaced by the success, not counted alongside it."""
    workflow = make_workflow(client, "counted")
    boom = make_shell_task(client, workflow["id"], "boom", "exit 1")
    run_id = run_now(client, workflow["id"])
    assert client.get("/api/stats/summary").json()["failed_24h"] == 1

    client.put(f"/api/tasks/{boom['id']}", json=task_body("boom", "printf ok"))
    client.post(f"/api/runs/{run_id}/resume")
    execute_queued_run(client)

    summary = client.get("/api/stats/summary").json()
    assert summary["runs_24h"] == 1 and summary["succeeded_24h"] == 1 and summary["failed_24h"] == 0
    assert sum(day["success"] + day["failed"] for day in client.get("/api/stats/daily").json()) == 1
