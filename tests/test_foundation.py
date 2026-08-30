"""v0.5 foundation: the migration, timezone-safe model helpers, snooze
suppression, the claim-time started_at guard, and the new schema bounds."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from runrail.db import Base
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun, _aware, now
from runrail.worker.queue import claim_next_run


def make_workflow(client, name, **extra):
    return client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1, **extra,
    }).json()


def make_shell_task(client, workflow_id, name, command):
    return client.post(f"/api/workflows/{workflow_id}/tasks", json={
        "name": name, "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    }).json()


def execute_queued_run(client):
    from runrail.db import SessionLocal
    from runrail.worker.service import execute_workflow_run
    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None
        execute_workflow_run(db, run)
        return run.id


def snooze(minutes: int, *, naive: bool = False) -> datetime:
    """A stored snooze instant. `naive` mimics what SQLite hands back."""
    value = now() + timedelta(minutes=minutes)
    return value.replace(tzinfo=None) if naive else value


def test_aware_tags_naive_values_as_utc():
    stamp = datetime(2026, 8, 30, 12, 0)
    assert _aware(stamp) == stamp.replace(tzinfo=timezone.utc)
    assert _aware(None) is None
    already = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    assert _aware(already) is already


def test_snoozed_compares_safely_against_sqlite_naive_datetimes():
    workflow = Workflow(name="quiet")
    assert workflow.snoozed is False                       # never snoozed
    workflow.snooze_until = snooze(60, naive=True)         # a raw > now() here raises TypeError
    assert workflow.snoozed is True
    workflow.snooze_until = snooze(-60, naive=True)
    assert workflow.snoozed is False                       # expires by the clock alone


def test_migration_creates_every_v05_column(client):
    from runrail.db import engine

    inspector = inspect(engine)
    columns = {table: {c["name"] for c in inspector.get_columns(table)}
               for table in ("workflows", "workflow_runs", "tasks", "task_runs", "run_notes")}
    assert {"snooze_until", "snooze_pauses_runs", "missed_run_grace_minutes",
            "missed_notified_at", "sla_minutes"} <= columns["workflows"]
    assert {"resume_count", "sla_breached_at"} <= columns["workflow_runs"]
    assert {"requires_approval", "approval_prompt"} <= columns["tasks"]
    assert {"resume_index", "approval_note", "approved_at"} <= columns["task_runs"]
    assert {"workflow_run_id", "body", "created_at", "updated_at"} <= columns["run_notes"]
    # Single user, no accounts: content and timing are kept, the name is not.
    assert "approved_by" not in columns["task_runs"] and "author" not in columns["run_notes"]
    # The notebook report and /latest lookups both filter artifacts by run.
    assert "ix_artifacts_workflow_run_id" in {i["name"] for i in
                                              inspector.get_indexes("artifacts")}


def test_snoozed_workflow_sends_no_failure_webhook(client, monkeypatch):
    from runrail.db import SessionLocal

    sent = []
    monkeypatch.setattr("runrail.notify._post",
                        lambda url, text, **fields: sent.append({"url": url, **fields}))
    workflow = make_workflow(client, "noisy", notify_webhook_url="https://hooks.example/x")
    task = make_shell_task(client, workflow["id"], "job", "exit 1")

    with SessionLocal() as db:
        db.get(Workflow, workflow["id"]).snooze_until = snooze(60)
        db.commit()
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    execute_queued_run(client)
    assert sent == []

    # Suppression is temporary, not a permanently muted webhook: once the
    # snooze lifts the same workflow talks again.
    with SessionLocal() as db:
        db.get(Workflow, workflow["id"]).snooze_until = None
        db.commit()
    make_shell_task(client, workflow["id"], "job2", "printf ok")
    client.delete(f"/api/tasks/{task['id']}")
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    execute_queued_run(client)
    assert [n["event"] for n in sent] == ["run_recovered"]


def test_snooze_survives_a_workflow_edit(client):
    """apply_update writes every WorkflowIn key, so snooze must stay out of it."""
    from runrail.db import SessionLocal

    workflow = make_workflow(client, "edited")
    with SessionLocal() as db:
        db.get(Workflow, workflow["id"]).snooze_until = snooze(60)
        db.commit()

    body = client.get(f"/api/workflows/{workflow['id']}").json()
    assert body["snoozed"] is True and body["snooze_pauses_runs"] is False
    client.put(f"/api/workflows/{workflow['id']}", json={
        "name": "edited", "enabled": True, "max_concurrent_runs": 1,
        "description": "renamed the description",
    })
    assert client.get(f"/api/workflows/{workflow['id']}").json()["snoozed"] is True


def test_claim_preserves_an_existing_started_at():
    """An approval gate releases a run back to queued; re-claiming it must not
    rewrite the timeline origin."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workflow = Workflow(name="gated"); db.add(workflow); db.flush()
        db.add(WorkflowRun(workflow_id=workflow.id, trigger_type=TriggerType.manual)); db.commit()

        first = claim_next_run(db)
        origin = first.started_at
        assert origin is not None

        first.status = RunStatus.queued; db.commit()
        again = claim_next_run(db)
        assert again.started_at == origin

        # A resume nulls started_at on purpose, and then gets a fresh one.
        again.status, again.started_at = RunStatus.queued, None
        db.commit()
        assert claim_next_run(db).started_at is not None


def test_workflow_schema_bounds():
    from runrail.schemas import WorkflowIn

    assert WorkflowIn(name="ok", missed_run_grace_minutes=1, sla_minutes=90).sla_minutes == 90
    for field in ("missed_run_grace_minutes", "sla_minutes"):
        with pytest.raises(ValidationError):
            WorkflowIn(name="bad", **{field: 0})


def test_snooze_window_is_bounded():
    from runrail.schemas import SnoozeIn

    assert SnoozeIn(until=snooze(60)).pause_runs is False
    with pytest.raises(ValidationError, match="future"):
        SnoozeIn(until=snooze(-1))
    with pytest.raises(ValidationError, match="30 days"):
        SnoozeIn(until=now() + timedelta(days=31))


def test_run_note_and_approval_schema_bounds():
    from runrail.schemas import ApprovalDecision, RunNoteIn

    assert RunNoteIn(body="  bad upstream file, ignore  ").body == "bad upstream file, ignore"
    for payload in ({"body": "   "}, {"body": ""}, {"body": "x" * 4001}):
        with pytest.raises(ValidationError):
            RunNoteIn(**payload)

    assert ApprovalDecision().note is None  # the decision alone is a valid body
    assert ApprovalDecision(note="counts check out").note == "counts check out"
    with pytest.raises(ValidationError):
        ApprovalDecision(note="x" * 2001)


def test_run_detail_carries_notes_and_new_run_fields(client):
    from runrail.db import SessionLocal
    from runrail.models import RunNote

    workflow = make_workflow(client, "annotated")
    make_shell_task(client, workflow["id"], "job", "printf ok")
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()
    assert run["resume_count"] == 0 and run["sla_breached_at"] is None

    with SessionLocal() as db:
        db.add(RunNote(workflow_run_id=run["id"], body="vendor re-sent the file"))
        db.commit()
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert [n["body"] for n in detail["notes"]] == ["vendor re-sent the file"]

    # Retention and workflow deletion both lean on the cascade rather than code.
    client.delete(f"/api/workflows/{workflow['id']}")
    with SessionLocal() as db:
        assert db.scalars(select(RunNote)).all() == []


def test_new_routers_do_not_shadow_the_spa_catch_all(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/nope").status_code == 404      # never falls through to index
    assert client.get("/").status_code == 200              # SPA (or the "not built" notice)


def test_waiting_approval_run_holds_its_concurrency_slot(client):
    """A gate that parks a run must not let the next iteration start underneath it."""
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, WorkflowRun
    from runrail.worker.queue import claim_next_run

    workflow = client.post("/api/workflows", json={
        "name": "gated", "enabled": True, "max_concurrent_runs": 1}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": "echo hi",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0})
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})
    client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}})

    with SessionLocal() as db:
        parked = claim_next_run(db)
        assert parked is not None
        parked.status = RunStatus.waiting_approval
        db.commit()
        # The second queued run must stay unclaimed while the first is parked.
        assert claim_next_run(db) is None
        # Releasing the gate frees the slot again.
        db.get(WorkflowRun, parked.id).status = RunStatus.success
        db.commit()
        assert claim_next_run(db) is not None


def test_a_run_waiting_on_approval_can_be_cancelled(client):
    """Approve and reject are a gate's only other exits — cancel must work too."""
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, TaskRun, TaskRunStatus, WorkflowRun

    workflow = client.post("/api/workflows", json={
        "name": "cancel-gate", "enabled": True, "max_concurrent_runs": 1}).json()
    task = client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": "echo hi",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0}).json()
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    with SessionLocal() as db:
        stored = db.get(WorkflowRun, run["id"])
        stored.status = RunStatus.waiting_approval
        db.add(TaskRun(workflow_run_id=run["id"], task_id=task["id"], attempt=0,
                       status=TaskRunStatus.awaiting_approval))
        db.commit()

    cancelled = client.post(f"/api/runs/{run['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    with SessionLocal() as db:
        stored = db.get(WorkflowRun, run["id"])
        assert stored.finished_at is not None
        gates = db.scalars(select_task_runs(run["id"])).all()
        assert all(t.status == TaskRunStatus.cancelled for t in gates)


def select_task_runs(run_id):
    from sqlalchemy import select

    from runrail.models import TaskRun
    return select(TaskRun).where(TaskRun.workflow_run_id == run_id)


def test_sla_late_message_measures_from_creation_not_claim(client, monkeypatch):
    """duration_seconds starts at claim; the deadline starts at creation, and the
    queue wait between them is exactly what a creation-relative SLA catches."""
    from datetime import timedelta

    from runrail import notify
    from runrail.db import SessionLocal
    from runrail.models import RunStatus, WorkflowRun, now

    posts = []
    monkeypatch.setattr(notify, "_post",
                        lambda url, text, **f: posts.append({"text": text, **f}))

    workflow = client.post("/api/workflows", json={
        "name": "late", "enabled": True, "max_concurrent_runs": 1,
        "sla_minutes": 1, "notify_webhook_url": "https://hooks.example/x"}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": "echo hi",
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0})
    run = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()

    with SessionLocal() as db:
        stored = db.get(WorkflowRun, run["id"])
        created = now() - timedelta(minutes=101)
        stored.created_at = created
        stored.started_at = created + timedelta(minutes=100)   # 100 min queued
        stored.finished_at = created + timedelta(minutes=101)
        stored.duration_seconds = 60.0                          # only 1 min executing
        stored.sla_breached_at = created + timedelta(minutes=1)
        stored.status = RunStatus.success
        db.commit()
        notify.notify_run_outcome(db, stored)

    late = [p for p in posts if p.get("event") == "sla_finished_late"]
    assert late, posts
    # 101 min from creation against a 1 min SLA = 100 min over, never negative.
    assert "100 min past" in late[0]["text"]
