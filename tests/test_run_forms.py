"""Run forms: a workflow's typed parameters, checked and defaulted on every
way a run starts, and handed to tasks as templates and environment."""

import json
import sys
from datetime import date, timedelta

import pytest

from runrail.db import SessionLocal
from runrail.run_parameters import ParameterError, check_definitions, local_today, resolve
from runrail.worker.queue import claim_next_run
from runrail.worker.service import execute_workflow_run

FORM = [
    {"name": "report_date", "type": "date", "default": "yesterday", "help": "The day to report on"},
    {"name": "region", "type": "choice", "choices": ["EU", "US", "APAC"], "default": "EU"},
    {"name": "limit", "type": "number", "default": 100},
    {"name": "dry_run", "type": "bool", "default": False},
    {"name": "note", "type": "text"},
]


def make(client, name="report", form=FORM, with_task=True, **extra):
    response = client.post("/api/workflows", json={"name": name, "enabled": True,
                                                   "run_parameters_json": form, **extra})
    assert response.status_code == 201, response.text
    workflow = response.json()
    if with_task:
        client.post(f"/api/workflows/{workflow['id']}/tasks", json={
            "name": "noop", "task_type": "shell", "depends_on_json": [],
            "command": f'"{sys.executable}" -c "pass"'})
    return workflow


def params(client, run):
    return client.get(f"/api/runs/{run['id']}").json()["parameters_json"]


def start(client, workflow, parameters=None):
    return client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": parameters or {}})


# ─── The form ───────────────────────────────────────────────────────────


def test_a_form_is_saved_with_defaults_in_their_types():
    checked = check_definitions([{"name": "n", "type": "number", "default": "2.50"},
                                 {"name": "flag", "type": "bool", "default": "yes"},
                                 {"name": "day", "type": "date", "default": "Today"}])
    assert [d["default"] for d in checked] == [2.5, True, "today"]
    assert check_definitions([]) is None and check_definitions(None) is None


@pytest.mark.parametrize("form, reason", [
    ([{"name": "run_id", "type": "text"}], "set by RunRail"),
    ([{"name": "2x", "type": "text"}], "letters, digits"),
    ([{"name": "a", "type": "text"}, {"name": "a", "type": "number"}], "twice"),
    ([{"name": "a", "type": "colour"}], "type must be one of"),
    ([{"name": "a", "type": "choice", "choices": []}], "at least one option"),
    ([{"name": "a", "type": "choice", "choices": ["x"], "default": "y"}], "must be one of x"),
    ([{"name": "a", "type": "number", "default": "lots"}], "not a number"),
    ([{"name": "ds", "type": "text"}], "must be a date"),
])
def test_a_form_that_cannot_work_is_refused(client, form, reason):
    response = client.post("/api/workflows", json={"name": "bad", "run_parameters_json": form})
    assert response.status_code == 422 and reason in response.text


def test_a_scheduled_workflow_needs_a_default_for_what_it_requires(client):
    form = [{"name": "customer", "type": "text", "required": True}]
    response = client.post("/api/workflows", json={"name": "s", "schedule_cron": "0 9 * * *",
                                                   "run_parameters_json": form})
    assert response.status_code == 422 and "give customer a default" in response.text
    workflow = make(client, "manual-only", form)
    response = client.put(f"/api/workflows/{workflow['id']}", json={"name": "manual-only",
                                                                    "schedule_cron": "0 9 * * *"})
    assert response.status_code == 422
    assert client.get(f"/api/workflows/{workflow['id']}").json()["schedule_cron"] is None


# ─── Starting runs ──────────────────────────────────────────────────────


def test_a_run_takes_defaults_and_converts_what_it_is_given(client):
    workflow = make(client)
    run = start(client, workflow, {"limit": "25", "dry_run": "yes", "region": "US",
                                   "extra": "kept"}).json()
    yesterday = (local_today(None) - timedelta(days=1)).isoformat()
    assert params(client, run) == {"report_date": yesterday, "region": "US", "limit": 25,
                                      "dry_run": True, "extra": "kept"}


@pytest.mark.parametrize("given, reason", [
    ({"region": "Mars"}, "region: must be one of EU, US, APAC"),
    ({"limit": "many"}, "not a number"),
    ({"report_date": "17/10/2026"}, "is not a date"),
    ({"dry_run": "maybe"}, "yes or no"),
])
def test_a_value_the_form_refuses_starts_nothing(client, given, reason):
    workflow = make(client)
    response = start(client, workflow, given)
    assert response.status_code == 422 and reason in response.json()["detail"]
    assert client.get("/api/runs").json() == []


def test_a_required_value_must_be_given(client):
    workflow = make(client, form=[{"name": "customer", "type": "text", "required": True,
                                   "label": "Customer"}])
    response = start(client, workflow)
    assert response.status_code == 422 and response.json()["detail"] == "Customer is required"
    assert start(client, workflow, {"customer": "acme"}).status_code == 201


def test_relative_dates_count_on_the_workflows_calendar():
    form = check_definitions([{"name": "d", "type": "date", "default": "yesterday"}])
    assert resolve(form, None, day=date(2026, 3, 1)) == {"d": "2026-02-28"}
    assert resolve(form, {"d": "2026-01-05"}, day=date(2026, 3, 1)) == {"d": "2026-01-05"}
    with pytest.raises(ParameterError):
        resolve(check_definitions([{"name": "x", "type": "text", "required": True}]), {}, day=date.today())
    # The schedule has nobody to ask: a missing required value is left out, not an error.
    assert resolve(check_definitions([{"name": "x", "type": "text", "required": True}]), {},
                   day=date.today(), strict=False) == {}


def test_a_backfill_counts_relative_dates_from_the_day_backfilled(client):
    workflow = make(client)
    runs = client.post(f"/api/workflows/{workflow['id']}/backfill",
                       json={"from": "2026-03-01", "to": "2026-03-02", "parameters": {"region": "US"}}).json()
    values = sorted((params(client, r) for r in runs), key=lambda p: p["ds"])
    assert [(p["ds"], p["report_date"], p["region"]) for p in values] == [
        ("2026-03-01", "2026-02-28", "US"), ("2026-03-02", "2026-03-01", "US")]


def test_a_scheduled_run_records_the_defaults_it_ran_with(client):
    from runrail.scheduler.service import enqueue_scheduled
    workflow = make(client, schedule_cron="0 9 * * *", schedule_timezone="Asia/Tokyo")
    enqueue_scheduled(workflow["id"])
    run = client.get(f"/api/runs?workflow_id={workflow['id']}").json()[0]
    expected = (local_today("Asia/Tokyo") - timedelta(days=1)).isoformat()
    assert params(client, run)["report_date"] == expected
    assert params(client, run)["region"] == "EU"


def test_a_retry_whose_values_no_longer_fit_says_why(client):
    workflow = make(client)
    run = start(client, workflow, {"region": "APAC"}).json()
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    form = [dict(p) for p in FORM]
    form[1]["choices"] = ["EU", "US"]
    client.put(f"/api/workflows/{workflow['id']}", json={"name": "report", "run_parameters_json": form})
    response = client.post(f"/api/runs/{run['id']}/retry")
    assert response.status_code == 422 and "no longer fit" in response.json()["detail"]


# ─── Tasks get them ─────────────────────────────────────────────────────


def test_tasks_get_the_values_in_templates_and_in_their_environment(client, tmp_path):
    workflow = make(client)
    script = tmp_path / "report.py"
    script.write_text("import json, os\n"
                      "params = json.loads(os.environ['RUNRAIL_PARAMS'])\n"
                      "print('limit', params['limit'] + 1)\n"
                      "print('dry', os.environ['RUNRAIL_PARAM_DRY_RUN'])\n"
                      "print('region', os.environ['RUNRAIL_PARAM_REGION'])\n")
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "templated", "task_type": "shell", "depends_on_json": [],
        "command": f'"{sys.executable}" -c "print(\'templated {{{{ region }}}} {{{{ limit }}}}\')"'})
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "script", "task_type": "shell", "depends_on_json": [],
        "command": f'"{sys.executable}" "{script}"'})
    run = start(client, workflow, {"region": "US", "dry_run": True}).json()
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["status"] == "success"
    out = {tr["task_name"]: client.get(f"/api/task-runs/{tr['id']}/stdout").text
           for tr in detail["task_runs"]}
    assert "templated US 100" in out["templated"]
    assert "limit 101" in out["script"] and "dry true" in out["script"] and "region US" in out["script"]


# ─── Workflow as code ───────────────────────────────────────────────────


def test_a_run_form_travels_in_the_yaml(client):
    from runrail.workflow_io import apply_workflows, export_workflows
    make(client)
    with SessionLocal() as db:
        exported = export_workflows(db)
    form = exported["workflows"][0]["run_parameters"]
    assert [p["name"] for p in form] == [p["name"] for p in FORM]
    exported["workflows"][0]["name"] = "copy"
    exported["workflows"][0].pop("key")
    with SessionLocal() as db:
        apply_workflows(db, {"workflows": exported["workflows"]})
    copy = next(w for w in client.get("/api/workflows").json() if w["name"] == "copy")
    assert copy["run_parameters_json"] == form
    bad = json.loads(json.dumps(exported["workflows"][0]))
    bad.update(name="bad", schedule_cron="0 9 * * *",
               run_parameters=[{"name": "who", "type": "text", "required": True}])
    with SessionLocal() as db, pytest.raises(ValueError, match="give who a default"):
        apply_workflows(db, {"workflows": [bad]})
