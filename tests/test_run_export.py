"""Sharing a run as one self-contained HTML file: escaping, the size budget,
and the sandbox around the embedded report."""

from pathlib import Path

import pytest

# Sibling import: pytest puts tests/ on sys.path, and the notebook fixtures
# belong with the reports they exist for.
from test_reports import attach_notebook, execute_queued_run, make_shell_task, make_workflow

from runrail import reports

_SENTINEL = "SECRET-TOKEN-abc123"


def run_workflow(client, name, tasks=(("job", "printf ok"),), **extra):
    workflow = make_workflow(client, name, **extra)
    for task_name, command in tasks:
        make_shell_task(client, workflow["id"], task_name, command)
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    return workflow, execute_queued_run(client)


def task_runs(client, run_id):
    return client.get(f"/api/runs/{run_id}").json()["task_runs"]


def write_log(client, run_id, text, stream="stdout", index=0):
    from runrail.db import SessionLocal
    from runrail.models import TaskRun

    task_run_id = task_runs(client, run_id)[index]["id"]
    with SessionLocal() as db:
        path = Path(getattr(db.get(TaskRun, task_run_id), f"{stream}_log_path"))
    path.write_text(text)
    return path


@pytest.fixture()
def renderer(monkeypatch):
    """nbconvert stand-in; the export cares about the report's bytes, not its
    contents."""
    def render(notebook):
        return "<html><body><h1>Quarterly numbers</h1></body></html>"

    monkeypatch.setattr(reports, "_render_html", render)
    monkeypatch.setattr(reports, "renderer_available", lambda: True)


def test_export_is_a_single_self_contained_file(client):
    _, run_id = run_workflow(client, "sales")
    body = client.get(f"/api/runs/{run_id}/export").text
    assert "<link rel=\"stylesheet\"" not in body
    assert "<script src=" not in body
    assert body.count('src="http') == 0
    # The one deliberate exception: the footer's link back to the live run.
    assert body.count('href="http') == 1
    assert "@font-face" not in body and "fonts.googleapis" not in body


def test_export_content_disposition_uses_a_safe_filename(client):
    _, run_id = run_workflow(client, "../../etc/passwd")
    response = client.get(f"/api/runs/{run_id}/export")
    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="runrail-etc_passwd-run-{run_id}.html"'
    assert "/" not in disposition.split("filename=")[1].strip('"')
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("text/html")


def test_export_escapes_untrusted_text(client):
    """Jinja's autoescape default is False and every value here is
    attacker-influenced — this is the guard for that one keyword argument."""
    _, run_id = run_workflow(client, "hostile",
                             tasks=(("<img src=x onerror=alert(1)>", "printf ok"),))
    write_log(client, run_id, "</script><script>alert(1)</script>")
    body = client.get(f"/api/runs/{run_id}/export", params={"report": 0}).text

    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_export_truncates_long_logs_and_marks_the_elision(client):
    _, run_id = run_workflow(client, "chatty")
    filler = "".join(f"line {index} padding padding padding\n" for index in range(160_000))
    write_log(client, run_id, f"FIRST-LINE\n{filler}LAST-LINE\n")

    body = client.get(f"/api/runs/{run_id}/export").text
    assert len(body) < 512 * 1024
    assert "FIRST-LINE" in body and "LAST-LINE" in body   # tail-weighted, but keeps a head
    assert "elided" in body and "MB elided" in body
    assert "/api/task-runs/" in body                      # the marker says where the rest is


def test_export_logs_none_omits_log_text(client):
    _, run_id = run_workflow(client, "quiet")
    write_log(client, run_id, _SENTINEL)
    assert _SENTINEL in client.get(f"/api/runs/{run_id}/export").text
    body = client.get(f"/api/runs/{run_id}/export", params={"logs": "none"}).text
    assert _SENTINEL not in body
    # The task rows survive; only their output is withheld.
    assert "<strong>job</strong>" in body and "<summary>stdout</summary>" not in body


def test_export_without_a_report_contains_no_script_at_all(client, renderer):
    """The variant to recommend when a mail gateway mangles the attachment."""
    _, run_id = run_workflow(client, "sales")
    attach_notebook(run_id, task_runs(client, run_id)[0]["id"])
    body = client.get(f"/api/runs/{run_id}/export", params={"report": 0}).text
    assert "<iframe" not in body
    assert "<script" not in body


def test_export_embeds_the_report_in_a_sandboxed_srcdoc(client, renderer):
    _, run_id = run_workflow(client, "sales")
    attach_notebook(run_id, task_runs(client, run_id)[0]["id"])
    body = client.get(f"/api/runs/{run_id}/export").text

    assert "srcdoc=" in body and 'sandbox="allow-scripts"' in body
    # The file opens as file:// on someone else's machine; allow-same-origin
    # would let the notebook's scripts read their local disk.
    assert "allow-same-origin" not in body
    # The report is a whole document, escaped into the attribute, not inlined.
    assert "&lt;h1&gt;Quarterly numbers&lt;/h1&gt;" in body


def test_export_drops_the_report_when_it_would_bust_the_budget(client, monkeypatch):
    monkeypatch.setattr(reports, "renderer_available", lambda: True)
    monkeypatch.setattr(reports, "_render_html", lambda notebook: "<html>" + "x" * 300_000)
    _, run_id = run_workflow(client, "sales")
    attach_notebook(run_id, task_runs(client, run_id)[0]["id"])

    body = client.get(f"/api/runs/{run_id}/export",
                      params={"max_bytes": reports.EXPORT_MIN_BYTES}).text
    # Dropped whole, never truncated: half a notebook is invalid HTML and the
    # recipient cannot tell until it renders wrong.
    assert "<iframe" not in body
    assert "was left out to keep this file under" in body
    assert len(body.encode()) < reports.EXPORT_MIN_BYTES


def test_export_rejects_a_run_that_is_still_in_progress(client):
    workflow = make_workflow(client, "slow")
    make_shell_task(client, workflow["id"], "job")
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()
    assert client.get(f"/api/runs/{run['id']}/export").status_code == 409
    execute_queued_run(client)
    assert client.get(f"/api/runs/{run['id']}/export").status_code == 200


def test_export_survives_missing_log_files(client):
    from runrail.db import SessionLocal
    from runrail.models import TaskRun

    _, run_id = run_workflow(client, "half-gone")
    task_run_id = task_runs(client, run_id)[0]["id"]
    with SessionLocal() as db:
        task_run = db.get(TaskRun, task_run_id)
        Path(task_run.stderr_log_path).unlink()
        task_run.stdout_log_path = None
        db.commit()

    body = client.get(f"/api/runs/{run_id}/export").text
    assert "(log file no longer available)" in body


def test_export_includes_every_task_run_including_retries_and_skips(client):
    workflow = make_workflow(client, "broken")
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "extract", "task_type": "shell", "command": "exit 3",
        "depends_on_json": [], "retries": 1, "retry_delay_seconds": 0})
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "load", "task_type": "shell", "command": "printf ok",
        "depends_on_json": ["extract"], "retries": 0, "retry_delay_seconds": 0})
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    run_id = execute_queued_run(client)

    body = client.get(f"/api/runs/{run_id}/export").text
    statuses = [tr["status"] for tr in task_runs(client, run_id)]
    assert statuses == ["failed", "failed", "skipped"]
    assert body.count("<strong>extract</strong>") == 2   # both attempts, not just the last
    assert "<strong>load</strong>" in body
    assert "attempt 2" in body and "A dependency did not succeed" in body


def test_export_template_ships_inside_the_package(client):
    """hatchling packages src/runrail, so a non-Python file inside it rides
    along in the wheel — asserted rather than trusted."""
    import runrail

    packaged = Path(runrail.__file__).parent / "web" / "run_export.html.j2"
    assert packaged.is_file()
    assert reports._TEMPLATE_PATH == packaged


def test_outputs_estimates_the_export_size(client, renderer):
    _, run_id = run_workflow(client, "sales")
    attach_notebook(run_id, task_runs(client, run_id)[0]["id"])
    body = client.get(f"/api/runs/{run_id}/outputs").json()

    assert body["workflow_name"] == "sales" and body["status"] == "success"
    assert body["reports"][0]["task_name"] == "job"
    estimates = body["estimated_export_bytes"]
    assert estimates["logs_none"] <= estimates["with_report"]
    assert estimates["without_report"] <= estimates["with_report"]
