"""Services: long-running processes the supervisor keeps up.

Real processes throughout — a stop that only changed a label is the bug this
codebase has already shipped once — with the supervisor's clock under the
test's control, so windows, backoff and stop timeouts are exercised without
waiting for them.
"""

import sys
import time
from datetime import UTC, datetime, timedelta

import psutil
import pytest
from portable import WINDOWS, process_alive
from test_interactions import make_shell_task, make_workflow, start_run

from runrail.db import SessionLocal
from runrail.environments import environment_ids_in_use
from runrail.models import RunStatus, Service, ServiceSession, SupervisorLease, WorkflowRun
from runrail.services import process as proc
from runrail.services.supervisor import ServiceSupervisor
from runrail.worker.queue import claim_next_run


def gone(pid: int) -> bool:
    """Not running. A zombie counts as gone: in these tests the test process is
    the parent of what a "dead" supervisor started, and never reaps it — a
    real orphan's parent is gone and the system reaps it."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def python(code: str) -> str:
    """A shell command running Python code; single quotes only inside."""
    assert '"' not in code
    return f'"{sys.executable}" -c "{code}"'


#: Prints, then stays up until stopped.
STAYS_UP = python("import time; print('up', flush=True); time.sleep(600)")
#: Ignores the polite stop, so only the forced kill ends it.
STUBBORN = python("import signal, time; "
                  "[signal.signal(getattr(signal, n), signal.SIG_IGN) "
                  "for n in ('SIGTERM', 'SIGBREAK') if hasattr(signal, n)]; "
                  "print('up', flush=True); time.sleep(600)")


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture()
def clock():
    # Monday 2026-10-19, 12:00 UTC.
    return Clock(datetime(2026, 10, 19, 12, 0, tzinfo=UTC))


@pytest.fixture()
def supervisor(client, clock, monkeypatch):
    # The API measures overrides against the same clock the supervisor uses.
    import runrail.api.routes_services as routes
    monkeypatch.setattr(routes, "now", clock)
    made = []

    def make() -> ServiceSupervisor:
        sup = ServiceSupervisor(clock=clock)
        made.append(sup)
        return sup

    yield make
    for sup in made:
        for live in list(sup.live.values()):
            proc.kill(live.pid)
        sup.shutdown()


def create(client, name="svc", command=STAYS_UP, **extra) -> dict:
    response = client.post("/api/services", json={"name": name, "command": command, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def service(client, service_id) -> dict:
    return client.get(f"/api/services/{service_id}").json()


def settle(sup: ServiceSupervisor, client, service_id, state: str, seconds=10) -> dict:
    """Tick until the service reports `state` (real time passes; the test clock
    does not, unless the test moves it)."""
    deadline = time.monotonic() + seconds
    while True:
        sup.tick()
        current = service(client, service_id)
        if current["state"] == state or time.monotonic() > deadline:
            assert current["state"] == state, current
            return current
        time.sleep(0.1)


def logged(client, service_id, text: str, seconds=15) -> None:
    """Wait for the service to write `text`. The stubborn command prints only
    after installing its handlers, so this is when ignoring a stop is real."""
    deadline = time.monotonic() + seconds
    while text not in client.get(f"/api/services/{service_id}/log").text:
        assert time.monotonic() < deadline, f"{text!r} never reached the log"
        time.sleep(0.05)


def sessions(client, service_id) -> list[dict]:
    return client.get(f"/api/services/{service_id}/sessions").json()


def test_a_manual_service_starts_and_stops_on_request(client, supervisor):
    sup = supervisor()
    svc = create(client)
    assert svc["restart_policy"] == "never", "started by hand, so not restarted by default"
    sup.tick()
    assert service(client, svc["id"])["state"] == "stopped"

    client.post(f"/api/services/{svc['id']}/start")
    running = settle(sup, client, svc["id"], "running")
    pid = running["current_session"]["pid"]
    assert process_alive(pid)
    logged(client, svc["id"], "up")

    client.post(f"/api/services/{svc['id']}/stop")
    settle(sup, client, svc["id"], "stopped")
    assert not process_alive(pid)
    assert sessions(client, svc["id"])[0]["stop_reason"] == "stopped_by_user"


def test_a_process_that_ignores_the_polite_stop_is_killed_at_the_timeout(client, supervisor, clock):
    sup = supervisor()
    svc = create(client, command=STUBBORN, start_mode="startup", stop_timeout_seconds=180)
    pid = settle(sup, client, svc["id"], "running")["current_session"]["pid"]
    logged(client, svc["id"], "up")

    client.post(f"/api/services/{svc['id']}/stop")
    sup.tick()
    stopping = service(client, svc["id"])
    assert stopping["state"] == "stopping" and "Force stop in 180s" in stopping["state_detail"]
    time.sleep(0.5)
    sup.tick()
    assert process_alive(pid), "killed before its stop timeout"

    clock.advance(seconds=181)
    settle(sup, client, svc["id"], "stopped")
    assert not process_alive(pid)
    assert sessions(client, svc["id"])[0]["stop_reason"] == "killed_after_timeout"


def test_a_crashing_service_backs_off_then_stops_as_a_crash_loop(client, supervisor, clock):
    sup = supervisor()
    svc = create(client, command=python("raise SystemExit(3)"), start_mode="startup")
    delays = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sup.tick()
        current = service(client, svc["id"])
        if current["state"] == "crash_loop":
            break
        if current["state"] == "backoff":
            wait = (datetime.fromisoformat(current["next_attempt_at"]) - clock.now).total_seconds()
            delays.append(wait)
            clock.advance(seconds=wait)
        time.sleep(0.05)
    assert delays[:4] == [1, 2, 4, 8], "the restart delay should double"
    assert "Crash loop" in current["state_detail"] and "code 3" in current["state_detail"]
    clock.advance(minutes=30)
    sup.tick()
    assert service(client, svc["id"])["state"] == "crash_loop", "a crash loop waits for a person"

    # Start clears it, and does not wait out a backoff.
    client.post(f"/api/services/{svc['id']}/start")
    sup.tick()
    assert len(sessions(client, svc["id"])) == 6


def test_a_clean_exit_under_on_failure_is_not_restarted(client, supervisor, clock):
    sup = supervisor()
    svc = create(client, command=python("print('done')"), start_mode="startup",
                 restart_policy="on_failure")
    settled = settle(sup, client, svc["id"], "stopped")
    assert "not restarted" in settled["state_detail"]
    clock.advance(minutes=5)
    sup.tick(); sup.tick()
    assert len(sessions(client, svc["id"])) == 1
    assert sessions(client, svc["id"])[0]["stop_reason"] == "exited"


def test_a_scheduled_service_follows_its_windows_and_a_click_lasts_until_the_next_change(
        client, supervisor, clock):
    sup = supervisor()
    svc = create(client, start_mode="schedule", timezone="UTC",
                 windows=[{"days": ["mon", "tue", "wed", "thu", "fri"],
                           "start": "08:00", "end": "17:00"}])
    assert svc["restart_policy"] == "always"
    settle(sup, client, svc["id"], "running")  # Monday 12:00: inside

    clock.now = datetime(2026, 10, 19, 17, 0, tzinfo=UTC)
    settle(sup, client, svc["id"], "stopped")
    assert sessions(client, svc["id"])[0]["stop_reason"] == "window_closed"

    # Started by hand at 19:00: runs through the night, then the window takes over.
    clock.now = datetime(2026, 10, 19, 19, 0, tzinfo=UTC)
    started = client.post(f"/api/services/{svc['id']}/start").json()
    assert started["override_until"].startswith("2026-10-20T08:00")
    settle(sup, client, svc["id"], "running")
    clock.now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    sup.tick()
    current = service(client, svc["id"])
    assert current["state"] == "running" and current["override_running"] is None
    assert len(sessions(client, svc["id"])) == 2, "the window opening must not restart it"

    # Stopped by hand mid-window: stays off until the window would have closed.
    stopped = client.post(f"/api/services/{svc['id']}/stop").json()
    assert stopped["override_until"].startswith("2026-10-20T17:00")
    settle(sup, client, svc["id"], "stopped")
    clock.now = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    settle(sup, client, svc["id"], "running")


def test_resume_schedule_drops_the_override(client, supervisor):
    sup = supervisor()
    svc = create(client, start_mode="startup")
    settle(sup, client, svc["id"], "running")
    client.post(f"/api/services/{svc['id']}/stop")
    settle(sup, client, svc["id"], "stopped")
    assert service(client, svc["id"])["override_until"] is None, "held until started again"
    client.post(f"/api/services/{svc['id']}/resume")
    settle(sup, client, svc["id"], "running")


def test_two_services_on_one_exclusive_lock_take_turns(client, supervisor, clock):
    """The night/day model swap: the next one waits for the first to exit."""
    sup = supervisor()
    night = create(client, "llama-night", command=STUBBORN, start_mode="startup",
                   lock_resource="gpu", stop_timeout_seconds=180)
    settle(sup, client, night["id"], "running")
    logged(client, night["id"], "up")
    day = create(client, "llama-day", start_mode="startup", lock_resource="gpu")
    waiting = settle(sup, client, day["id"], "waiting_lock")
    assert waiting["state_detail"] == "Waiting for gpu · held by service llama-night"

    client.post(f"/api/services/{night['id']}/stop")
    time.sleep(0.3)
    sup.tick()
    assert service(client, day["id"])["state"] == "waiting_lock", \
        "a stopping service still holds the lock"
    clock.advance(seconds=181)
    settle(sup, client, night["id"], "stopped")
    settle(sup, client, day["id"], "running")


def test_services_and_workflows_share_locks_without_preempting(client, supervisor):
    sup = supervisor()
    gpu_job = make_workflow(client, "gpu-job", lock_resource="gpu", lock_mode="exclusive")
    make_shell_task(client, gpu_job["id"], "t", "exit 0")

    model = create(client, "model", start_mode="startup", lock_resource="gpu")
    settle(sup, client, model["id"], "running")
    start_run(client, gpu_job["id"])
    with SessionLocal() as db:
        assert claim_next_run(db) is None, "a run took the GPU from a running model"

    client.post(f"/api/services/{model['id']}/stop")
    settle(sup, client, model["id"], "stopped")
    with SessionLocal() as db:
        run = claim_next_run(db)
        assert run is not None
        run_id = run.id

    # The model's turn comes while the run holds the GPU: it waits, never preempts…
    client.post(f"/api/services/{model['id']}/start")
    waiting = settle(sup, client, model["id"], "waiting_lock")
    assert "held by workflow gpu-job" in waiting["state_detail"]
    # …and while it waits, no NEW run jumps ahead of it.
    start_run(client, gpu_job["id"])
    with SessionLocal() as db:
        db.get(WorkflowRun, run_id).status = RunStatus.success
        db.commit()
        assert claim_next_run(db) is None, "a new run overtook the waiting model"
    settle(sup, client, model["id"], "running")


def test_only_one_supervisor_acts(client, supervisor):
    first, second = supervisor(), supervisor()
    svc = create(client, start_mode="startup")
    first.tick(); second.tick(); first.tick(); second.tick()
    time.sleep(0.3)
    first.tick(); second.tick()
    running = [s for s in sessions(client, svc["id"]) if s["stopped_at"] is None]
    assert len(running) == 1 and not second.live


def test_a_new_supervisor_cleans_up_after_one_that_died(client, supervisor, clock):
    dead = supervisor()
    svc = create(client, start_mode="startup")
    old_pid = settle(dead, client, svc["id"], "running")["current_session"]["pid"]
    dead.live.clear()  # it died: nobody is watching that process any more
    with SessionLocal() as db:
        db.get(SupervisorLease, "services").heartbeat_at = clock.now - timedelta(minutes=1)
        db.commit()

    heir = supervisor()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        heir.tick()
        current = service(client, svc["id"])
        if current["state"] == "running" and current["current_session"]["pid"] != old_pid:
            break
        time.sleep(0.1)
    assert gone(old_pid), "the orphaned process was left running"
    assert process_alive(service(client, svc["id"])["current_session"]["pid"])
    assert [s["stop_reason"] for s in sessions(client, svc["id"])][1] == "interrupted"


def test_shutting_down_stops_services_and_they_return_on_the_next_start(client, supervisor):
    sup = supervisor()
    svc = create(client, start_mode="startup")
    pid = settle(sup, client, svc["id"], "running")["current_session"]["pid"]
    sup.shutdown()
    assert not process_alive(pid)
    assert sessions(client, svc["id"])[0]["stop_reason"] == "runrail_shutdown"
    settle(supervisor(), client, svc["id"], "running")


def test_deleting_a_running_service_stops_its_process(client, supervisor):
    sup = supervisor()
    svc = create(client, start_mode="startup")
    pid = settle(sup, client, svc["id"], "running")["current_session"]["pid"]
    assert client.delete(f"/api/services/{svc['id']}").status_code == 204
    deadline = time.monotonic() + 10
    while process_alive(pid) and time.monotonic() < deadline:
        sup.tick()
        time.sleep(0.1)
    assert not process_alive(pid)
    assert not sup.live


def test_an_environment_a_service_runs_from_counts_as_in_use(client, supervisor):
    environment = client.post("/api/environments", json={
        "name": "env", "executable": sys.executable}).json()
    sup = supervisor()
    svc = create(client, start_mode="startup", environment_id=environment["id"])
    settle(sup, client, svc["id"], "running")
    with SessionLocal() as db:
        assert environment["id"] in environment_ids_in_use(db)


def test_the_supervisor_status_reports_whether_anything_supervises(client, supervisor):
    assert client.get("/api/services/supervisor").json()["active"] is False
    supervisor().tick()
    assert client.get("/api/services/supervisor").json()["active"] is True


@pytest.mark.parametrize("body, status", [
    ({"start_mode": "schedule"}, 422),
    ({"start_mode": "schedule", "windows": [{"days": ["mon"], "start": "9:00", "end": "17:00"}]}, 422),
    ({"timezone": "Mars/Base"}, 422),
    ({"stop_timeout_seconds": 0}, 422),
    ({"command": "   "}, 422),
])
def test_invalid_services_are_refused(client, body, status):
    response = client.post("/api/services", json={"name": "bad", "command": "x", **body})
    assert response.status_code == status, response.text


def test_names_are_unique(client):
    create(client, "same")
    assert client.post("/api/services", json={"name": "same", "command": "x"}).status_code == 409


def test_editing_the_schedule_drops_an_override(client, supervisor, clock):
    svc = create(client, start_mode="startup")
    client.post(f"/api/services/{svc['id']}/stop")
    assert service(client, svc["id"])["override_running"] is False
    edited = client.put(f"/api/services/{svc['id']}", json={
        "name": "svc", "command": STAYS_UP, "start_mode": "manual"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["override_running"] is None


@pytest.mark.skipif(WINDOWS, reason="POSIX process groups")
def test_a_stop_reaches_what_the_shell_started(client, supervisor, tmp_path):
    """The shell is only the parent: `sh -c "python ...; echo"` keeps the shell
    alive, and stopping must reach the Python underneath."""
    pidfile = tmp_path / "pid"
    sup = supervisor()
    svc = create(client, start_mode="startup", command=(
        python(f"import os, time; open(r'{pidfile}', 'w').write(str(os.getpid())); "
               "time.sleep(600)") + "; echo done"))
    settle(sup, client, svc["id"], "running")
    deadline = time.monotonic() + 10
    while not (pidfile.exists() and pidfile.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    client.post(f"/api/services/{svc['id']}/stop")
    settle(sup, client, svc["id"], "stopped")
    deadline = time.monotonic() + 3
    while process_alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_alive(child)


def test_session_rows_follow_their_service(client):
    svc = create(client)
    with SessionLocal() as db:
        db.add(ServiceSession(service_id=svc["id"]))
        db.commit()
    client.delete(f"/api/services/{svc['id']}")
    with SessionLocal() as db:
        assert db.query(ServiceSession).count() == 0
        assert db.query(Service).count() == 0




def test_services_travel_with_a_yaml_export(client):
    from runrail.workflow_io import apply_workflows, export_workflows
    svc = create(client, "sync", start_mode="schedule", timezone="Europe/London",
                 windows=[{"days": ["mon", "fri"], "start": "08:00", "end": "17:00"}],
                 lock_resource="disk", stop_timeout_seconds=120)
    with SessionLocal() as db:
        exported = export_workflows(db)
    assert exported["services"][0]["windows"] == svc["windows"]
    assert exported["services"][0]["key"] == svc["key"]

    client.put(f"/api/services/{svc['id']}", json={"name": "renamed", "command": "x",
                                                   "stop_timeout_seconds": 5})
    with SessionLocal() as db:
        summary = apply_workflows(db, exported)
    assert summary == {"created": [], "updated": ["service sync"]}
    restored = service(client, svc["id"])
    assert (restored["name"], restored["stop_timeout_seconds"]) == ("sync", 120)
    assert len(client.get("/api/services").json()) == 1, "applying after a rename duplicated it"

    exported["services"][0]["windows"] = []
    with SessionLocal() as db, pytest.raises(ValueError, match="at least one window"):
        apply_workflows(db, exported)
