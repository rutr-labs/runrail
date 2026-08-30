"""Duration trends: the per-task window, and the median/MAD rule that decides
"slower than usual" without crying wolf."""

from statistics import mean, stdev


def make_workflow(client, name):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()


def make_task(client, workflow_id, name):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": "true",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    }).json()


def seed_durations(workflow_id, task_id, durations, status="success"):
    """One run per sample, oldest first — the shape real history has."""
    from runrail.db import SessionLocal
    from runrail.models import TaskRun, TriggerType, WorkflowRun

    with SessionLocal() as db:
        seeded = []
        for value in durations:
            run = WorkflowRun(workflow_id=workflow_id, status="success",
                              trigger_type=TriggerType.manual)
            db.add(run); db.flush()
            task_run = TaskRun(workflow_run_id=run.id, task_id=task_id, status=status,
                               duration_seconds=value)
            db.add(task_run); db.flush()
            seeded.append((run.id, task_run.id))
        db.commit()
        return seeded


def trends(client, workflow_id, **params):
    response = client.get(f"/api/workflows/{workflow_id}/task-durations", params=params)
    assert response.status_code == 200, response.text
    return {entry["task_name"]: entry for entry in response.json()}


def test_the_window_is_per_task_not_a_flat_limit(client):
    """A rarely-run task inside a busy workflow is the one you most want a
    trend for; a flat LIMIT would starve it."""
    workflow = make_workflow(client, "mixed")
    rare = make_task(client, workflow["id"], "monthly_load")
    busy = make_task(client, workflow["id"], "five_minute_poll")
    seed_durations(workflow["id"], rare["id"], [10.0, 11.0, 12.0])
    seed_durations(workflow["id"], busy["id"], [float(index) for index in range(30)])

    body = trends(client, workflow["id"], window=20)
    assert len(body["monthly_load"]["samples"]) == 3
    assert len(body["five_minute_poll"]["samples"]) == 20
    # The newest 20, not the oldest.
    assert body["five_minute_poll"]["samples"][-1]["duration_seconds"] == 29.0


def test_only_successful_task_runs_form_the_baseline(client):
    """A task that fails fast in 2s would otherwise drag the median down and
    make every healthy run look slow."""
    workflow = make_workflow(client, "mixed-outcomes")
    task = make_task(client, workflow["id"], "job")
    seed_durations(workflow["id"], task["id"], [40.0, 40.0, 40.0, 40.0, 40.0])
    for status in ("failed", "cancelled", "skipped"):
        seed_durations(workflow["id"], task["id"], [2.0], status=status)

    entry = trends(client, workflow["id"])["job"]
    assert [s["duration_seconds"] for s in entry["samples"]] == [40.0] * 5
    assert entry["median"] == 40.0


def test_samples_come_back_oldest_first_with_their_ids(client):
    workflow = make_workflow(client, "ordering")
    task = make_task(client, workflow["id"], "job")
    seeded = seed_durations(workflow["id"], task["id"], [1.0, 2.0, 3.0])

    samples = trends(client, workflow["id"])["job"]["samples"]
    assert [s["duration_seconds"] for s in samples] == [1.0, 2.0, 3.0]
    assert [(s["workflow_run_id"], s["task_run_id"]) for s in samples] == seeded


def test_statistics_match_hand_computed_fixtures(client):
    workflow = make_workflow(client, "fixtures")
    task = make_task(client, workflow["id"], "job")
    seed_durations(workflow["id"], task["id"], [10.0, 20.0, 30.0, 40.0, 50.0])

    entry = trends(client, workflow["id"])["job"]
    assert entry["median"] == 30.0
    assert entry["p90"] == 46.0                      # 40 + 0.6 * (50 - 40)
    assert entry["spread"] == 14.826                 # 1.4826 * MAD(10)
    assert entry["last"] == 50.0 and entry["slow_ratio"] == 1.67
    assert entry["slow"] is False                    # 50 < 30 + 3 * 14.826


def test_five_samples_are_the_floor_for_a_slow_verdict(client):
    workflow = make_workflow(client, "young")
    four = make_task(client, workflow["id"], "four_runs")
    five = make_task(client, workflow["id"], "five_runs")
    seed_durations(workflow["id"], four["id"], [10.0, 10.0, 10.0, 100.0])
    seed_durations(workflow["id"], five["id"], [10.0, 10.0, 10.0, 10.0, 100.0])

    body = trends(client, workflow["id"])
    assert body["four_runs"]["slow"] is False        # two runs cannot establish "usual"
    assert body["five_runs"]["slow"] is True
    assert body["five_runs"]["slow_ratio"] == 10.0


def test_a_rock_steady_task_is_never_flagged(client):
    """MAD is 0 here; without the spread floor, any increase at all reads as
    slow."""
    workflow = make_workflow(client, "steady")
    task = make_task(client, workflow["id"], "job")
    seed_durations(workflow["id"], task["id"], [2.0] * 7 + [2.4])

    entry = trends(client, workflow["id"])["job"]
    assert entry["spread"] == 0.5                    # 0.25 * median, not 1.4826 * 0
    assert entry["slow"] is False


def test_the_absolute_floor_beats_the_ratio(client):
    """Nobody cares that a task went from 0.3s to 1.2s, whatever the multiple."""
    workflow = make_workflow(client, "floors")
    tiny = make_task(client, workflow["id"], "tiny")
    real = make_task(client, workflow["id"], "real")
    seed_durations(workflow["id"], tiny["id"], [0.3] * 6 + [1.2])
    seed_durations(workflow["id"], real["id"], [40.0] * 19 + [130.0])

    body = trends(client, workflow["id"])
    assert body["tiny"]["slow_ratio"] == 4.0 and body["tiny"]["slow"] is False
    assert body["real"]["slow"] is True


def test_one_historical_outlier_does_not_mask_a_later_regression(client):
    """The whole reason for MAD over stdev: a single 40-minute run inflates a
    standard deviation enough to hide every regression after it."""
    workflow = make_workflow(client, "outlier")
    task = make_task(client, workflow["id"], "job")
    values = [38.0, 40.0, 42.0, 39.0, 41.0, 40.0, 38.0, 42.0, 40.0, 41.0,
              39.0, 40.0, 42.0, 38.0, 41.0, 2400.0, 40.0, 39.0, 41.0, 130.0]
    seed_durations(workflow["id"], task["id"], values)

    entry = trends(client, workflow["id"])["job"]
    assert entry["median"] == 40.0 and entry["spread"] == 10.0
    assert entry["slow"] is True
    # The same rule built on mean + 3 * stdev would miss it entirely.
    assert entry["last"] < mean(values) + 3 * stdev(values)


def test_degenerate_and_unknown_workflows(client):
    workflow = make_workflow(client, "cold-start")
    make_task(client, workflow["id"], "never_run")

    assert client.get(f"/api/workflows/{workflow['id']}/task-durations").json() == []
    assert client.get("/api/workflows/999/task-durations").status_code == 404
    for window in (1, 101):
        response = client.get(f"/api/workflows/{workflow['id']}/task-durations",
                              params={"window": window})
        assert response.status_code == 422
