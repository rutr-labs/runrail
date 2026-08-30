"""Notebook reports: the lazy render + disk cache, the security headers every
entry point shares, and the /latest resolver."""

import json
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

from runrail import reports


def stub_report(notebook) -> str:
    """What the stub renderer produces: it names its source, so a test can tell
    which of a run's notebooks was picked."""
    return (f"<html><body><h1>Quarterly numbers</h1><p>{Path(notebook).stem}</p>"
            "<script>plot()</script></body></html>")


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1, **extra,
    }).json()


def make_shell_task(client, workflow_id, name, command="printf ok"):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    }).json()


def execute_queued_run(client):
    from runrail.db import SessionLocal
    from runrail.worker.queue import claim_next_run
    from runrail.worker.service import execute_workflow_run
    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None
        execute_workflow_run(db, run)
        return run.id


def notebook_source(heading="Quarterly numbers", valid=True) -> str:
    """A minimal executed notebook. Written by hand rather than with nbformat:
    nbformat is not a declared dependency, and a notebook is just JSON."""
    if not valid:
        return "{not json at all"
    return json.dumps({
        "nbformat": 4, "nbformat_minor": 5, "metadata": {},
        "cells": [
            {"cell_type": "markdown", "id": "a1", "metadata": {}, "source": f"# {heading}"},
            {"cell_type": "code", "id": "b2", "metadata": {}, "source": "print('ok')",
             "execution_count": 1, "outputs": [
                 {"output_type": "stream", "name": "stdout", "text": "ok\n"}]},
        ],
    })


def attach_notebook(run_id, task_run_id, name="build_report.ipynb", **kwargs):
    """Register an .ipynb exactly as _run_task does when papermill produces one."""
    from runrail.config import get_settings
    from runrail.db import SessionLocal
    from runrail.models import Artifact, ArtifactType

    directory = get_settings().artifacts_dir.resolve() / str(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(notebook_source(**kwargs))
    with SessionLocal() as db:
        artifact = Artifact(task_run_id=task_run_id, workflow_run_id=run_id, name=name,
                            artifact_type=ArtifactType.notebook, path=str(path),
                            size_bytes=path.stat().st_size)
        db.add(artifact); db.commit()
        return artifact.id


def notebook_run(client, workflow_name="daily-sales", task="build_report", **kwargs):
    """A finished run carrying a notebook artifact."""
    workflow = make_workflow(client, workflow_name)
    make_shell_task(client, workflow["id"], task)
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)
    task_run_id = client.get(f"/api/runs/{run_id}").json()["task_runs"][0]["id"]
    return workflow, run_id, attach_notebook(run_id, task_run_id, **kwargs)


@pytest.fixture()
def renderer(monkeypatch):
    """Stands in for nbconvert, which is an optional extra.

    Everything the pipeline itself owns — the cache, the headers, /latest, the
    export — is exercised through this; the tests below marked importorskip are
    the ones that genuinely need nbconvert.
    """
    calls: list[str] = []

    def render(notebook: Path) -> str:
        calls.append(str(notebook))
        return stub_report(notebook)

    monkeypatch.setattr(reports, "_render_html", render)
    monkeypatch.setattr(reports, "renderer_available", lambda: True)
    return calls


def test_report_renders_lazily_and_caches_a_second_artifact(client, renderer):
    workflow, run_id, source_id = notebook_run(client)
    assert client.get(f"/api/runs/{run_id}/outputs").json()["reports"][0]["rendered"] is False

    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Quarterly numbers" in response.text
    assert renderer == [str(Path(client.get(f"/api/artifacts/{source_id}").json()["path"]))]

    from runrail.config import get_settings
    cached = [a for a in client.get("/api/artifacts", params={"workflow_run_id": run_id}).json()
              if a["artifact_type"] == "html"]
    assert len(cached) == 1 and cached[0]["name"] == "build_report.html"
    path = Path(cached[0]["path"])
    assert path.is_file() and path.parent == get_settings().artifacts_dir.resolve() / str(run_id)
    assert cached[0]["size_bytes"] == path.stat().st_size


def test_report_is_served_from_cache_not_rerendered(client, renderer):
    _, run_id, _ = notebook_run(client)
    first = client.get(f"/api/runs/{run_id}/report")
    second = client.get(f"/api/runs/{run_id}/report")
    assert len(renderer) == 1
    assert first.text == second.text
    assert client.get(f"/api/runs/{run_id}/outputs").json()["reports"][0]["rendered"] is True


def test_report_response_carries_the_sandbox_csp(client, renderer):
    _, run_id, _ = notebook_run(client)
    headers = client.get(f"/api/runs/{run_id}/report").headers
    csp = headers["content-security-policy"]
    for directive in ("sandbox allow-scripts", "default-src 'none'", "connect-src 'none'",
                      "form-action 'none'", "frame-ancestors 'self'", "img-src data:"):
        assert directive in csp
    # allow-scripts together with allow-same-origin is equivalent to no sandbox
    # at all — the one change that would silently undo the whole mechanism.
    assert "allow-same-origin" not in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "private, no-store"


def test_latest_report_headers_are_identical_to_the_run_report(client, renderer):
    workflow, run_id, _ = notebook_run(client)
    run = client.get(f"/api/runs/{run_id}/report")
    latest = client.get(f"/api/workflows/{workflow['id']}/latest-report/html")
    assert (run.status_code, latest.status_code) == (200, 200)
    for header in ("content-security-policy", "x-content-type-options",
                   "referrer-policy", "cache-control"):
        assert run.headers[header] == latest.headers[header], header


def test_report_without_a_renderer_explains_how_to_install_it(client, monkeypatch):
    monkeypatch.setattr(reports, "renderer_available", lambda: False)
    _, run_id, _ = notebook_run(client)
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 503
    assert response.json()["code"] == "renderer_missing"
    assert "runrail[notebook]" in response.json()["detail"]


def test_a_failed_render_never_touches_the_run(client, monkeypatch):
    """The contract behind rendering lazily: the renderer runs in a request
    handler and cannot reach TaskRun.status or WorkflowRun.status."""
    def explode(notebook):
        raise reports.ReportError(422, "render_failed", "ValueError: bad notebook")

    monkeypatch.setattr(reports, "_render_html", explode)
    monkeypatch.setattr(reports, "renderer_available", lambda: True)
    _, run_id, _ = notebook_run(client, valid=False)
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "success"

    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 422 and response.json()["code"] == "render_failed"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "success"
    assert [tr["status"] for tr in detail["task_runs"]] == ["success"]


def test_report_selects_a_notebook_by_task_name(client, renderer):
    """Two notebook tasks in one run: the default is the lowest Task.id, and
    ?task= pins the other — which is what makes the choice shareable."""
    workflow = make_workflow(client, "two-reports")
    make_shell_task(client, workflow["id"], "build_report")
    make_shell_task(client, workflow["id"], "board pack")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)
    for task_run in client.get(f"/api/runs/{run_id}").json()["task_runs"]:
        attach_notebook(run_id, task_run["id"], name=f"{task_run['task_name']}.ipynb",
                        heading=task_run["task_name"])

    assert "build_report" in client.get(f"/api/runs/{run_id}/report").text
    pinned = [entry for entry in client.get(f"/api/runs/{run_id}/outputs").json()["reports"]
              if entry["task_name"] == "board pack"][0]
    assert pinned["report_url"].endswith("?task=board%20pack")
    assert "board pack" in client.get(pinned["report_url"]).text

    missing = client.get(f"/api/runs/{run_id}/report", params={"task": "nope"})
    assert missing.status_code == 404 and missing.json()["code"] == "no_notebook"


def test_render_refuses_an_oversized_notebook(client, renderer):
    from runrail.config import get_settings
    from runrail.db import SessionLocal
    from runrail.models import Artifact

    _, run_id, source_id = notebook_run(client)
    with SessionLocal() as db:
        db.get(Artifact, source_id).size_bytes = reports._MAX_SOURCE_BYTES + 1
        db.commit()

    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 413 and response.json()["code"] == "notebook_too_large"
    directory = get_settings().artifacts_dir.resolve() / str(run_id)
    assert list(directory.glob("*.html")) == [] and list(directory.glob("*.part")) == []
    assert renderer == []


def test_report_reports_a_source_removed_by_retention(client, renderer):
    _, run_id, source_id = notebook_run(client)
    Path(client.get(f"/api/artifacts/{source_id}").json()["path"]).unlink()
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 410 and response.json()["code"] == "source_removed"


def test_run_without_a_notebook_says_so(client, renderer):
    workflow = make_workflow(client, "shell-only")
    make_shell_task(client, workflow["id"], "greet")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 404 and response.json()["code"] == "no_notebook"
    assert client.get(f"/api/runs/{run_id}/outputs").json()["reports"] == []


def test_rendered_report_downloads_as_an_attachment(client, renderer):
    """A notebook-authored HTML document must never be served as a renderable
    same-origin page; /download is the path that guarantees it."""
    _, run_id, _ = notebook_run(client)
    client.get(f"/api/runs/{run_id}/report")
    html_id = [a["id"] for a in client.get("/api/artifacts",
                                           params={"workflow_run_id": run_id}).json()
               if a["artifact_type"] == "html"][0]
    disposition = client.get(f"/api/artifacts/{html_id}/download").headers["content-disposition"]
    assert disposition.startswith("attachment")


def test_concurrent_first_views_render_once(client, monkeypatch):
    """Two viewers arriving together must produce one render and one row."""
    from runrail.db import SessionLocal
    from runrail.models import Artifact, ArtifactType

    started = threading.Barrier(2)
    calls: list[str] = []

    def slow_render(notebook):
        calls.append(str(notebook))
        return stub_report(notebook)

    monkeypatch.setattr(reports, "_render_html", slow_render)
    monkeypatch.setattr(reports, "renderer_available", lambda: True)
    _, run_id, source_id = notebook_run(client)

    def view():
        started.wait(timeout=5)
        with SessionLocal() as db:
            reports.render_report(db, db.get(Artifact, source_id))

    threads = [threading.Thread(target=view) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)

    assert len(calls) == 1
    rows = [a for a in client.get("/api/artifacts", params={"workflow_run_id": run_id}).json()
            if a["artifact_type"] == ArtifactType.html.value]
    assert len(rows) == 1 and Path(rows[0]["path"]).is_file()


def test_force_render_replaces_the_cached_report(client, renderer):
    _, run_id, source_id = notebook_run(client)
    client.get(f"/api/runs/{run_id}/report")
    cached = Path([a for a in client.get("/api/artifacts",
                                         params={"workflow_run_id": run_id}).json()
                   if a["artifact_type"] == "html"][0]["path"])
    cached.write_text("stale")

    response = client.post(f"/api/artifacts/{source_id}/render")
    assert response.status_code == 200 and response.json()["artifact_type"] == "html"
    assert cached.read_text() == stub_report(cached.with_suffix(".ipynb"))
    assert len(renderer) == 2
    assert len([a for a in client.get("/api/artifacts",
                                      params={"workflow_run_id": run_id}).json()
                if a["artifact_type"] == "html"]) == 1


def test_render_endpoint_rejects_a_non_notebook_artifact(client, renderer):
    from runrail.db import SessionLocal
    from runrail.models import Artifact, ArtifactType

    _, run_id, _ = notebook_run(client)
    with SessionLocal() as db:
        other = Artifact(workflow_run_id=run_id, name="data.csv", path="/tmp/data.csv",
                         artifact_type=ArtifactType.file)
        db.add(other); db.commit()
        other_id = other.id
    assert client.post(f"/api/artifacts/{other_id}/render").status_code == 409


# --- the stable /latest URL ------------------------------------------------


def finished_run(client, workflow_id, status, with_notebook=True, name="build_report.ipynb"):
    """A terminal run of an existing workflow, optionally carrying a notebook."""
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, TaskRun, TaskRunStatus, TriggerType, WorkflowRun, now

    with SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow_id, trigger_type=TriggerType.manual,
                          status=RunStatus(status), started_at=now(), finished_at=now())
        db.add(run); db.flush()
        task_id = client.get(f"/api/workflows/{workflow_id}/tasks").json()[0]["id"]
        task_run = TaskRun(workflow_run_id=run.id, task_id=task_id,
                           status=TaskRunStatus.success, started_at=now(), finished_at=now())
        db.add(task_run); db.commit()
        run_id, task_run_id = run.id, task_run.id
    if with_notebook:
        attach_notebook(run_id, task_run_id, name=name)
    return run_id


def test_latest_report_resolves_the_newest_successful_run(client, renderer):
    workflow, first_run, _ = notebook_run(client, "sales")
    finished_run(client, workflow["id"], "failed")
    newest = finished_run(client, workflow["id"], "success")

    body = client.get(f"/api/workflows/{workflow['id']}/latest-report").json()
    assert body["run_id"] == newest and body["run_id"] != first_run
    assert body["task_name"] == "build_report"
    assert body["permalink"] == f"/api/runs/{newest}/report"
    assert body["newer_successful_run_id"] is None


def test_latest_report_skips_a_successful_run_without_a_report(client, renderer):
    workflow, run_id, _ = notebook_run(client, "sales")
    bare = finished_run(client, workflow["id"], "success", with_notebook=False)

    body = client.get(f"/api/workflows/{workflow['id']}/latest-report").json()
    assert body["run_id"] == run_id
    # Visible rather than silent: the reader is told a newer success exists.
    assert body["newer_successful_run_id"] == bare


def test_latest_report_ignores_runs_that_are_not_successful(client, renderer):
    workflow, run_id, _ = notebook_run(client, "sales")
    for status in ("failed", "cancelled", "running"):
        finished_run(client, workflow["id"], status)
    assert client.get(f"/api/workflows/{workflow['id']}/latest-report").json()["run_id"] == run_id


def test_latest_report_404_codes(client, renderer):
    assert client.get("/api/workflows/ghost/latest-report").json()["code"] == "no_such_workflow"

    workflow = make_workflow(client, "never-green")
    make_shell_task(client, workflow["id"], "job", "exit 1")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    execute_queued_run(client)
    body = client.get(f"/api/workflows/{workflow['id']}/latest-report")
    assert body.status_code == 404 and body.json()["code"] == "no_successful_run"

    finished_run(client, workflow["id"], "success", with_notebook=False)
    body = client.get(f"/api/workflows/{workflow['id']}/latest-report")
    assert body.json()["code"] == "no_report_in_any_successful_run"


def test_latest_report_resolves_a_workflow_by_id_and_by_name(client, renderer):
    workflow, run_id, _ = notebook_run(client, "daily sales")
    by_id = client.get(f"/api/workflows/{workflow['id']}/latest-report").json()
    by_name = client.get(f"/api/workflows/{quote('daily sales')}/latest-report").json()
    assert by_id == by_name and by_id["run_id"] == run_id


def test_latest_report_marks_staleness(client, renderer):
    workflow, run_id, _ = notebook_run(client, "sales")
    for _ in range(5):
        finished_run(client, workflow["id"], "failed", with_notebook=False)
    body = client.get(f"/api/workflows/{workflow['id']}/latest-report").json()
    assert body["stale"] is True and body["failed_since"] == 5
    assert body["workflow_enabled"] is True


def test_latest_report_is_fresh_when_nothing_has_failed(client, renderer):
    workflow, _, _ = notebook_run(client, "sales")
    body = client.get(f"/api/workflows/{workflow['id']}/latest-report").json()
    assert body["stale"] is False and body["failed_since"] == 0
    assert body["age_seconds"] < 60


def test_latest_report_html_renders_on_demand_when_not_yet_cached(client, renderer):
    workflow, run_id, _ = notebook_run(client, "sales")
    response = client.get(f"/api/workflows/{workflow['id']}/latest-report/html")
    assert response.status_code == 200 and "Quarterly numbers" in response.text
    assert len(renderer) == 1
    assert client.get(f"/api/runs/{run_id}/outputs").json()["reports"][0]["rendered"] is True


def test_run_permalink_survives_a_workflow_rename(client, renderer):
    workflow, run_id, _ = notebook_run(client, "sales")
    client.get(f"/api/runs/{run_id}/report")
    client.put(f"/api/workflows/{workflow['id']}", json={
        "name": "sales-renamed", "enabled": True, "max_concurrent_runs": 1})
    assert client.get(f"/api/runs/{run_id}/report").status_code == 200
    assert client.get("/api/workflows/sales/latest-report").status_code == 404


# --- the real renderer -----------------------------------------------------


def test_nbconvert_produces_a_document_with_no_external_references(client):
    """Proves the mathjax/require/jquery trait blanking still holds; a template
    that starts pulling assets would show up here rather than as an empty page."""
    import re

    pytest.importorskip("nbconvert")
    _, run_id, _ = notebook_run(client)
    body = client.get(f"/api/runs/{run_id}/report").text
    assert "Quarterly numbers" in body
    assert re.findall(r'(?:src|href)=["\']https?://', body) == []
    assert "runrailReportHeight" in body  # the height reporter survives nbconvert


def test_nbconvert_reports_a_corrupt_notebook_as_render_failed(client):
    pytest.importorskip("nbconvert")
    _, run_id, _ = notebook_run(client, valid=False)
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 422 and response.json()["code"] == "render_failed"
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "success"
