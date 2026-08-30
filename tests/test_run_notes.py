"""Run notes: an append-only annotation thread, its cheap table flag, and the
cascade that keeps retention code-free."""

from datetime import datetime, timedelta, timezone


def make_workflow(client, name):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
    }).json()


def make_run(client, workflow_id):
    from runrail.db import SessionLocal
    from runrail.models import TriggerType, WorkflowRun
    with SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow_id, status="failed",
                          trigger_type=TriggerType.manual)
        db.add(run); db.commit()
        return run.id


def add_note(client, run_id, body, author=None):
    response = client.post(f"/api/runs/{run_id}/notes", json={"body": body, "author": author})
    assert response.status_code == 201, response.text
    return response.json()


def test_notes_read_oldest_first_as_a_timeline(client):
    workflow = make_workflow(client, "annotated")
    run_id = make_run(client, workflow["id"])

    add_note(client, run_id, "bad upstream file, ignore", author="shivam")
    add_note(client, run_id, "vendor re-sent it, re-ran as #520")

    notes = client.get(f"/api/runs/{run_id}/notes").json()
    assert [n["body"] for n in notes] == ["bad upstream file, ignore",
                                          "vendor re-sent it, re-ran as #520"]
    assert [n["author"] for n in notes] == ["shivam", None]  # unsigned round-trips as null


def test_notes_on_an_unknown_run_are_404(client):
    assert client.get("/api/runs/999/notes").status_code == 404
    assert client.post("/api/runs/999/notes", json={"body": "x"}).status_code == 404


def test_note_bodies_are_bounded(client):
    workflow = make_workflow(client, "bounds")
    run_id = make_run(client, workflow["id"])
    for payload in ({"body": ""}, {"body": "   "}, {"body": "x" * 4001},
                    {"body": "ok", "author": "a" * 81}):
        assert client.post(f"/api/runs/{run_id}/notes", json=payload).status_code == 422


def test_editing_a_note_bumps_updated_at_and_keeps_created_at(client):
    workflow = make_workflow(client, "edited")
    run_id = make_run(client, workflow["id"])
    note = add_note(client, run_id, "watching this one")

    updated = client.put(f"/api/run-notes/{note['id']}",
                         json={"body": "resolved: vendor re-sent", "author": "shivam"})
    assert updated.status_code == 200
    body = updated.json()
    assert body["body"] == "resolved: vendor re-sent" and body["author"] == "shivam"
    assert body["created_at"] == note["created_at"]
    assert body["updated_at"] > note["updated_at"]  # drives the "edited" marker


def test_deleting_a_note_removes_it_from_the_thread(client):
    workflow = make_workflow(client, "deleted")
    run_id = make_run(client, workflow["id"])
    keep = add_note(client, run_id, "keep me")
    drop = add_note(client, run_id, "drop me")

    assert client.delete(f"/api/run-notes/{drop['id']}").status_code == 204
    assert [n["id"] for n in client.get(f"/api/runs/{run_id}/notes").json()] == [keep["id"]]
    assert client.delete(f"/api/run-notes/{drop['id']}").status_code == 404


def test_notes_cascade_away_with_their_run(client):
    from runrail.db import SessionLocal
    from runrail.models import RunNote, WorkflowRun

    workflow = make_workflow(client, "cascade")
    run_id = make_run(client, workflow["id"])
    add_note(client, run_id, "context that dies with the run")

    with SessionLocal() as db:
        db.delete(db.get(WorkflowRun, run_id)); db.commit()
    with SessionLocal() as db:
        assert db.query(RunNote).count() == 0


def test_retention_cleanup_takes_the_notes_with_it(client):
    """The FK cascade is why maintenance.py needs no change; assert it rather
    than trust it."""
    from runrail.db import SessionLocal
    from runrail.maintenance import cleanup_runs
    from runrail.models import RunNote, WorkflowRun

    workflow = make_workflow(client, "retention")
    run_id = make_run(client, workflow["id"])
    add_note(client, run_id, "ignored on purpose")

    with SessionLocal() as db:
        db.get(WorkflowRun, run_id).created_at = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40))
        db.commit()
    with SessionLocal() as db:
        assert cleanup_runs(db, older_than_days=30).runs_deleted == 1
    with SessionLocal() as db:
        assert db.query(RunNote).count() == 0


def test_run_detail_embeds_the_thread(client):
    workflow = make_workflow(client, "detail")
    run_id = make_run(client, workflow["id"])
    add_note(client, run_id, "first")
    add_note(client, run_id, "second")

    detail = client.get(f"/api/runs/{run_id}").json()
    assert [n["body"] for n in detail["notes"]] == ["first", "second"]


def test_summary_flags_annotated_runs_without_fetching_every_note(client):
    workflow = make_workflow(client, "summary")
    other = make_workflow(client, "summary-other")
    flagged = make_run(client, workflow["id"])
    make_run(client, workflow["id"])  # unannotated runs never appear
    elsewhere = make_run(client, other["id"])

    assert client.get("/api/runs/notes/summary").json() == {}

    add_note(client, flagged, "x" * 200)
    add_note(client, flagged, "a later note")
    add_note(client, elsewhere, "different workflow")

    summary = client.get("/api/runs/notes/summary").json()
    assert set(summary) == {str(flagged), str(elsewhere)}
    assert summary[str(flagged)]["count"] == 2
    # The preview is the OLDEST note (the reason), truncated for a hover title.
    assert summary[str(flagged)]["preview"] == "x" * 120

    scoped = client.get("/api/runs/notes/summary", params={"workflow_id": workflow["id"]}).json()
    assert set(scoped) == {str(flagged)}
