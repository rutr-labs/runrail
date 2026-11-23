"""Workflows that run after other workflows.

One rule: once every workflow it runs after has succeeded since it last
started, it starts. No dates: pipelines that run every few minutes keep their
own place in tracking tables, and need RunRail only to start them at the right
moment, once, without piling up."""

from datetime import timedelta

import yaml
from test_interactions import drain, make_shell_task, make_workflow, start_run

from runrail import chaining
from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun, WorkflowUpstream, now
from runrail.workflow_io import apply_workflows, export_workflows


def workflow_with_task(client, name, command="exit 0", **extra):
    workflow = make_workflow(client, name, **extra)
    make_shell_task(client, workflow["id"], "t", command)
    return workflow


def put(client, workflow_id, **fields):
    """An update, as the edit form sends one: the name always goes along."""
    name = client.get(f"/api/workflows/{workflow_id}").json()["name"]
    return client.put(f"/api/workflows/{workflow_id}", json={"name": name, **fields})


def chain(client, workflow_id, *upstream_ids):
    response = put(client, workflow_id, after=list(upstream_ids))
    assert response.status_code == 200, response.text
    return response.json()


def runs_of(workflow_id) -> list[WorkflowRun]:
    with SessionLocal() as db:
        return list(db.query(WorkflowRun).filter(WorkflowRun.workflow_id == workflow_id)
                    .order_by(WorkflowRun.id))


def test_a_success_starts_what_runs_after_it(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    assert chain(client, transform["id"], ingest["id"])["after"] == [ingest["id"]]

    upstream_run = start_run(client, ingest["id"])
    drain(client)

    [run] = runs_of(transform["id"])
    assert run.trigger_type == TriggerType.upstream
    assert run.triggered_by_run_id == upstream_run
    assert run.status == RunStatus.success  # drain ran it too


def test_a_failure_starts_nothing(client):
    ingest = workflow_with_task(client, "ingest", "exit 3")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    start_run(client, ingest["id"])
    drain(client)
    assert runs_of(transform["id"]) == []


def test_with_several_upstreams_it_waits_for_all_of_them(client):
    sales = workflow_with_task(client, "ingest-sales")
    crm = workflow_with_task(client, "ingest-crm")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], sales["id"], crm["id"])

    start_run(client, sales["id"])
    drain(client)
    assert runs_of(transform["id"]) == []  # crm has not succeeded yet

    crm_run = start_run(client, crm["id"])
    drain(client)
    [run] = runs_of(transform["id"])
    assert run.triggered_by_run_id == crm_run  # the success that completed the set

    # Started since: the next round needs both again.
    start_run(client, sales["id"])
    drain(client)
    assert len(runs_of(transform["id"])) == 1


def test_one_run_waits_however_many_successes_arrive(client):
    """A 15-minute upstream ahead of a slow downstream: one queued run, which
    picks up everything new when it starts. Never a pile."""
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    with SessionLocal() as db:
        for _ in range(3):
            db.add(WorkflowRun(workflow_id=ingest["id"], status=RunStatus.success,
                               trigger_type=TriggerType.schedule, finished_at=now()))
            db.commit()
            run = db.query(WorkflowRun).order_by(WorkflowRun.id.desc()).first()
            chaining.after_success(db, run)
    queued = [r for r in runs_of(transform["id"]) if r.status == RunStatus.queued]
    assert len(queued) == 1


def test_a_success_while_it_runs_queues_exactly_one_more(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    with SessionLocal() as db:
        started = now() - timedelta(minutes=5)
        db.add(WorkflowRun(workflow_id=transform["id"], status=RunStatus.running,
                           trigger_type=TriggerType.upstream, created_at=started, started_at=started))
        db.commit()
    start_run(client, ingest["id"])
    from test_interactions import execute_next_run
    execute_next_run(client)  # the ingest run; transform is held by max_concurrent_runs
    statuses = [r.status for r in runs_of(transform["id"])]
    assert statuses == [RunStatus.running, RunStatus.queued]


def test_a_success_from_before_the_link_does_not_count(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    start_run(client, ingest["id"])
    drain(client)
    chain(client, transform["id"], ingest["id"])
    with SessionLocal() as db:
        assert chaining.sweep(db) == []
    assert runs_of(transform["id"]) == []


def test_starting_it_by_hand_counts_as_starting(client):
    """A manual run processed what was new, so an upstream success from
    before it does not start another."""
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    with SessionLocal() as db:
        db.add(WorkflowRun(workflow_id=ingest["id"], status=RunStatus.success,
                           trigger_type=TriggerType.schedule, finished_at=now()))
        db.commit()
    start_run(client, transform["id"])
    drain(client)
    with SessionLocal() as db:
        assert chaining.sweep(db) == []
    assert [r.trigger_type for r in runs_of(transform["id"])] == [TriggerType.manual]


def test_the_minute_check_starts_what_the_immediate_one_missed(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    with SessionLocal() as db:  # a success recorded with no check after it
        db.add(WorkflowRun(workflow_id=ingest["id"], status=RunStatus.success,
                           trigger_type=TriggerType.schedule, finished_at=now()))
        db.commit()
        [started] = chaining.sweep(db)
        assert started.workflow_id == transform["id"]
        assert chaining.sweep(db) == []  # and only once


def test_a_switched_off_downstream_is_not_started(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform", enabled=False)
    chain(client, transform["id"], ingest["id"])
    start_run(client, ingest["id"])
    drain(client)
    assert runs_of(transform["id"]) == []


def test_schedule_and_after_are_one_or_the_other(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    both = put(client, transform['id'], schedule_cron="*/15 * * * *", after=[ingest["id"]])
    assert both.status_code == 422 and "not both" in both.text
    chain(client, transform["id"], ingest["id"])
    # A schedule added later, without clearing `after`, is refused too.
    later = put(client, transform['id'], schedule_cron="0 * * * *")
    assert later.status_code == 422
    # Switching over in one save is fine.
    switched = put(client, transform['id'], schedule_cron="0 * * * *", after=[])
    assert switched.status_code == 200 and switched.json()["after"] == []


def test_loops_itself_and_unknown_workflows_are_refused(client):
    a = workflow_with_task(client, "a")
    b = workflow_with_task(client, "b")
    c = workflow_with_task(client, "c")
    chain(client, b["id"], a["id"])
    chain(client, c["id"], b["id"])
    loop = put(client, a['id'], after=[c["id"]])
    assert loop.status_code == 422 and "loop" in loop.text
    itself = put(client, a['id'], after=[a["id"]])
    assert itself.status_code == 422
    unknown = put(client, a['id'], after=[9999])
    assert unknown.status_code == 422


def test_a_required_parameter_needs_a_default_when_nobody_is_asked(client):
    ingest = workflow_with_task(client, "ingest")
    transform = make_workflow(client, "transform", run_parameters_json=[
        {"name": "region", "type": "text", "required": True}])
    response = put(client, transform['id'], after=[ingest["id"]])
    assert response.status_code == 422 and "after other workflows" in response.text


def test_deleting_an_upstream_unlinks_it(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    assert client.delete(f"/api/workflows/{ingest['id']}").status_code == 204
    assert client.get(f"/api/workflows/{transform['id']}").json()["after"] == []
    with SessionLocal() as db:
        assert db.query(WorkflowUpstream).count() == 0


def test_editing_something_else_keeps_what_upstreams_have_done(client):
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    with SessionLocal() as db:
        made = db.query(WorkflowUpstream).one().created_at
    put(client, transform['id'], description="nightly", after=[ingest["id"]])
    with SessionLocal() as db:
        assert db.query(WorkflowUpstream).one().created_at == made


def test_the_chain_view_says_what_it_waits_for(client):
    sales = workflow_with_task(client, "ingest-sales")
    crm = workflow_with_task(client, "ingest-crm", "exit 1")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], sales["id"], crm["id"])
    start_run(client, sales["id"])
    start_run(client, crm["id"])
    drain(client)
    view = client.get(f"/api/workflows/{transform['id']}/chain").json()
    by_name = {u["name"]: u for u in view["after"]}
    assert by_name["ingest-sales"]["done"] is True
    assert by_name["ingest-crm"]["done"] is False
    assert by_name["ingest-crm"]["last_status"] == "failed"
    assert view["queued"] is False
    upstream_view = client.get(f"/api/workflows/{sales['id']}/chain").json()
    assert upstream_view["then"] == [{"id": transform["id"], "name": "transform"}]


def test_yaml_carries_after_by_name_and_resolves_names_later_in_the_file(client):
    data = yaml.safe_load("""
workflows:
  - name: transform
    after: [ingest-sales, ingest-crm]
    tasks: [{name: t, task_type: shell, command: "exit 0"}]
  - name: ingest-sales
    tasks: [{name: t, task_type: shell, command: "exit 0"}]
  - name: ingest-crm
    tasks: [{name: t, task_type: shell, command: "exit 0"}]
""")
    with SessionLocal() as db:
        apply_workflows(db, data)
        transform = db.query(Workflow).filter(Workflow.name == "transform").one()
        names = {db.get(Workflow, i).name for i in transform.after}
        assert names == {"ingest-sales", "ingest-crm"}
        exported = {w["name"]: w for w in export_workflows(db)["workflows"]}
    assert exported["transform"]["after"] == ["ingest-crm", "ingest-sales"]
    assert "after" not in exported["ingest-crm"]
    assert "schedule_cron" not in exported["transform"]


def test_yaml_can_turn_a_chain_around_in_one_apply(client):
    def apply(text):
        with SessionLocal() as db:
            apply_workflows(db, yaml.safe_load(text))
    task = "tasks: [{name: t, task_type: shell, command: 'exit 0'}]"
    apply(f"workflows:\n  - {{name: a, {task}}}\n  - {{name: b, after: [a], {task}}}\n")
    apply(f"workflows:\n  - {{name: a, after: [b], {task}}}\n  - {{name: b, {task}}}\n")
    with SessionLocal() as db:
        a = db.query(Workflow).filter(Workflow.name == "a").one()
        b = db.query(Workflow).filter(Workflow.name == "b").one()
        assert a.after == [b.id] and b.after == []


def test_yaml_refuses_an_unknown_upstream(client):
    data = {"workflows": [{"name": "transform", "after": ["nope"],
                           "tasks": [{"name": "t", "task_type": "shell", "command": "exit 0"}]}]}
    with SessionLocal() as db:
        try:
            apply_workflows(db, data)
        except ValueError as exc:
            assert "nope" in str(exc)
        else:
            raise AssertionError("an unknown upstream was accepted")


def test_a_chained_run_queued_before_a_pause_does_not_start(client):
    """Like a scheduled one: switched off while it waited, it is dropped."""
    ingest = workflow_with_task(client, "ingest")
    transform = workflow_with_task(client, "transform")
    chain(client, transform["id"], ingest["id"])
    with SessionLocal() as db:
        db.add(WorkflowRun(workflow_id=ingest["id"], status=RunStatus.success,
                           trigger_type=TriggerType.schedule, finished_at=now()))
        db.commit()
        chaining.sweep(db)
    put(client, transform['id'], enabled=False)
    drain(client)
    assert all(r.status != RunStatus.success for r in runs_of(transform["id"]))
