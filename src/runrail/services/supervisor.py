"""The service supervisor: makes each service's process match what it should be.

Once a second it reconciles every service: works out whether it should be
running (its mode, its windows, any override), compares that with its process,
and starts, stops, restarts or waits. Everything it decides is written to the
service row — state, a one-line explanation, when it will next try — so the API
and the UI report the supervisor's view rather than guessing at it.

Exactly one supervisor acts, by holding a lease row with a heartbeat: `serve`
and a separate `worker` may both be running, and two supervisors would each
start a copy. A supervisor that takes over from one that died first deals with
what it left behind: processes still running from a recorded session are
stopped (identified by pid AND start time, never by pid alone), and the services
are started fresh, with output going to a log again.

Overrides carry the decisions that outlive a tick. A Start or Stop click is
one; so is the supervisor's own hold after a clean exit under `on_failure`, or
after a crash loop. An override on a scheduled service lasts until its next
scheduled change; on the others, until someone changes it.
"""

import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runrail import notify, runtime_settings
from runrail.api.ws import manager as _ws_manager
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.environments import environment_usable_now
from runrail.models import (
    Environment,
    RestartPolicy,
    Service,
    ServiceSession,
    ServiceStartMode,
    ServiceState,
    ServiceStopReason,
    SupervisorLease,
    _aware,
    now,
)
from runrail.paths import confined
from runrail.services import process as proc
from runrail.services.locks import HOLDING, holder_description, try_claim
from runrail.services.windows import is_active, next_change
from runrail.worker import runners

log = logging.getLogger("runrail.services")

TICK_SECONDS = 1.0
LEASE_NAME = "services"
#: A lease not renewed for this long belongs to a supervisor that died.
LEASE_STALE_AFTER = timedelta(seconds=15)
LEASE_RENEW_EVERY = timedelta(seconds=5)
#: This many unexpected exits inside the window is a crash loop.
CRASH_LOOP_EXITS = 5
CRASH_LOOP_WINDOW = timedelta(minutes=10)
#: A process that stayed up this long resets the restart backoff.
STABLE_AFTER = timedelta(seconds=60)
MAX_BACKOFF_SECONDS = 300
#: How often a running service's CPU and memory are recorded.
RESOURCE_SAMPLE_SECONDS = 5.0

_UNEXPECTED = (ServiceStopReason.exited, ServiceStopReason.crashed,
               ServiceStopReason.failed_to_start)


@dataclass
class _Live:
    service_id: int
    session_id: int
    pid: int
    created: float | None
    started: datetime
    stop_timeout: int
    process: subprocess.Popen | None = None  # None for a process adopted from a dead supervisor
    pump: threading.Thread | None = None
    stop_reason: ServiceStopReason | None = None
    kill_at: datetime | None = None
    killed: bool = False
    extra: dict = field(default_factory=dict)


def desired(service: Service, at: datetime) -> tuple[bool, ServiceStopReason]:
    """Whether the service should be running at `at`, and — when it should not —
    the reason a running process would be stopped with."""
    until = _aware(service.override_until)
    if service.override_running is not None and (until is None or at < until):
        return service.override_running, ServiceStopReason.stopped_by_user
    if service.start_mode == ServiceStartMode.startup:
        return True, ServiceStopReason.stopped_by_user
    if service.start_mode == ServiceStartMode.schedule:
        return (is_active(service.windows_json or [], service.timezone, at),
                ServiceStopReason.window_closed)
    return False, ServiceStopReason.stopped_by_user


def override_until(service: Service, at: datetime) -> datetime | None:
    """How long a click (or a hold) lasts: to the next scheduled change for a
    scheduled service, indefinitely otherwise."""
    if service.start_mode != ServiceStartMode.schedule:
        return None
    return next_change(service.windows_json or [], service.timezone, at)


def set_override(service: Service, running: bool, reason: str, at: datetime,
                 keep: bool = False) -> None:
    """Make the service run (or not) until its next scheduled change. Where the
    override would only restate the service's own default, it is cleared —
    unless `keep`: the supervisor's own holds are also the record of WHY the
    service is stopped, and a manual service's clean exit is one even though
    "not running" is already its default."""
    service.override_running, service.override_until, service.override_reason = None, None, None
    if desired(service, at)[0] == running and not keep:
        return
    service.override_running = running
    service.override_until = override_until(service, at)
    service.override_reason = reason


def clear_hold(service: Service, at: datetime) -> None:
    """A person acted (Start, Resume, an edit): drop any override, and start
    the crash count afresh so the exits before now do not decide the next one."""
    service.override_running = service.override_until = service.override_reason = None
    service.attempts_from = at


class ServiceSupervisor:
    def __init__(self, clock=now):
        self.clock = clock
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.live: dict[int, _Live] = {}
        self.holding = False
        self._renewed: datetime | None = None
        self._stop = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a bad tick must never take the supervisor down
                log.exception("service supervisor tick failed")
            self._stop.wait(TICK_SECONDS)
        self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        """Stop every service this supervisor started, each with its own stop
        timeout, then give up the lease. They start again with RunRail."""
        if self.live:
            print(f"Stopping {len(self.live)} service(s)…", flush=True)
        for live in self.live.values():
            if live.stop_reason is None:
                self._begin_stop(live, ServiceStopReason.runrail_shutdown, self.clock())
        while self.live:
            at = self.clock()
            with SessionLocal() as db:
                # A stop can take minutes; without heartbeats a standby
                # supervisor would take over and restart everything mid-stop.
                self._lease(db, at)
                for service_id, live in list(self.live.items()):
                    self._snapshot(live)
                    self._progress_stop(live, at)
                    if self._finished(live):
                        self._close_session(db, live, at)
                        service = db.get(Service, service_id)
                        if service is not None:
                            self._set_state(db, service, ServiceState.stopped, "RunRail stopped")
                        del self.live[service_id]
                db.commit()
            if self.live:
                time.sleep(0.2)
        with SessionLocal() as db:
            db.execute(update(SupervisorLease).where(
                SupervisorLease.name == LEASE_NAME, SupervisorLease.owner == self.owner)
                .values(heartbeat_at=datetime(1970, 1, 1, tzinfo=UTC)))
            db.commit()
        self.holding = False

    # ── one pass ───────────────────────────────────────────────────────────
    def tick(self) -> None:
        at = self.clock()
        with SessionLocal() as db:
            if not self._lease(db, at):
                if self.holding:  # lost it: another supervisor is acting now
                    for live in self.live.values():
                        if live.stop_reason is None:
                            self._begin_stop(live, ServiceStopReason.interrupted, at)
                self.holding = False
                self._progress_all_without_rows(db, at)
                return
            if not self.holding:
                self._take_over(db, at)
                self.holding = True
            services = {service.id: service for service in db.scalars(select(Service))}
            for service_id in [s for s in self.live if s not in services]:
                live = self.live[service_id]
                if live.stop_reason is None:
                    self._begin_stop(live, ServiceStopReason.deleted, at)
            self._progress_all_without_rows(db, at, keep=set(services))
            for service in services.values():
                try:
                    self._reconcile(db, service, at)
                except Exception:  # one service's trouble is not the others'
                    db.rollback()
                    log.exception("reconciling service %s failed", service.id)

    def _progress_all_without_rows(self, db: Session, at: datetime, keep=frozenset()) -> None:
        for service_id, live in list(self.live.items()):
            if service_id in keep:
                continue
            self._progress_stop(live, at)
            if self._finished(live):
                self._close_session(db, live, at)
                del self.live[service_id]
        db.commit()

    def _reconcile(self, db: Session, service: Service, at: datetime) -> None:
        # An expired override is cleared rather than left to be re-read forever.
        until = _aware(service.override_until)
        if service.override_running is not None and until is not None and at >= until:
            service.override_running = service.override_until = service.override_reason = None
            db.commit()
        should_run, stop_reason = desired(service, at)
        live = self.live.get(service.id)

        if live is not None:
            self._snapshot(live)
        restarting = False
        if live is not None and self._finished(live):
            restarting = live.stop_reason == ServiceStopReason.restarted and should_run
            self._finish(db, service, live, at)
            # Settled on the next pass, from the state this one leaves: a hold,
            # a backoff or a pending restart. Carrying on here would act on the
            # decision made before the exit and start it straight back up. A
            # restart is the exception: it goes straight on to the start, still
            # holding its lock, or a queued run takes the GPU in the gap.
            if not restarting:
                return
            live = None
        if live is not None:
            if live.stop_reason is None:
                if service.restart_requested:
                    self._begin_stop(live, ServiceStopReason.restarted, at)
                elif not should_run:
                    self._begin_stop(live, stop_reason, at)
            if live.stop_reason is not None:
                self._progress_stop(live, at)
                # Written once: the UI counts down from state_since itself.
                self._set_state(db, service, ServiceState.stopping,
                                "Force-stopping" if live.killed else "Asked to stop")
            else:
                self._set_state(db, service, ServiceState.running, None)
                self._sample(db, service, live, at)
            return

        if service.restart_requested:
            service.restart_requested = False
            db.commit()
        if not should_run:
            crash = service.override_reason == "crash_loop"
            self._set_state(db, service,
                            ServiceState.crash_loop if crash else ServiceState.stopped,
                            service.state_detail if crash else self._idle_detail(service, at),
                            next_attempt_at=None)
            return
        retry_at = _aware(service.next_attempt_at)
        if retry_at is not None and at < retry_at:
            self._set_state(db, service, ServiceState.backoff, service.state_detail,
                            next_attempt_at=retry_at)
            return
        if runtime_settings.current("paused"):
            # A service already waiting for its lock keeps that state (and so
            # its place in the queue, which is ordered by state_since).
            kept = service.state if service.state == ServiceState.waiting_lock else ServiceState.stopped
            self._set_state(db, service, kept, "RunRail is paused (Settings)")
            return
        waiting_for = self._environment_wait(db, service)
        if waiting_for:
            # Not a start attempt: a build in progress is not the service's
            # fault, and five of these in fifteen seconds is not a crash loop.
            self._set_state(db, service, ServiceState.backoff, waiting_for, next_attempt_at=None)
            return
        if not try_claim(db, service, at, restarting=restarting):
            db.refresh(service)
            if not service.lock_resource:
                # Nothing to wait for: the row is stale (left mid-start by an
                # error); a stopped state lets the next pass try again.
                self._set_state(db, service, ServiceState.stopped, None)
                return
            holder = holder_description(db, service)
            self._set_state(db, service, ServiceState.waiting_lock,
                            f"Waiting for {service.lock_resource}"
                            + (f" · held by {holder}" if holder else ""))
            return
        db.refresh(service)
        self._start(db, service, at)

    def _environment_wait(self, db: Session, service: Service) -> str | None:
        if not service.environment_id:
            return None
        environment = db.get(Environment, service.environment_id)
        if environment is None or environment_usable_now(environment):
            return None
        return (f"Waiting for environment '{environment.name}' to be ready "
                f"({environment.last_error or environment.status.value})")

    def _snapshot(self, live: _Live) -> None:
        """Windows only: remember every process under the service's, merged
        across ticks. A child whose parent has exited drops out of the tree
        and could not be found again; the last snapshot alone also missed a
        program started by a `cmd /c start` that was gone before the first
        tick. Entries are kept only while they still name the same process."""
        if os.name != "nt":
            return
        known = dict(live.extra.get("descendants", ()))
        known.update(proc.descendants(live.pid))
        live.extra["descendants"] = [(pid, started) for pid, started in known.items()
                                     if proc.same_process(pid, started)]

    # ── starting and stopping ──────────────────────────────────────────────
    def _start(self, db: Session, service: Service, at: datetime) -> None:
        session = ServiceSession(service_id=service.id, started_at=at, owner=self.owner,
                                 command=service.command)
        db.add(session)
        db.flush()
        log_path = proc.log_directory(service.id) / f"session_{session.id}.log"
        session.log_path = str(log_path)
        try:
            spec = proc.launch_spec(db, service)
            process, pump = proc.start(spec, log_path)
        except Exception as exc:
            proc.note(log_path, f"could not start: {exc}")
            session.stopped_at, session.stop_reason = at, ServiceStopReason.failed_to_start
            db.commit()
            self._after_unexpected_exit(db, service, at, None, f"Could not start: {exc}")
            return
        session.pid, session.process_created = process.pid, proc.created(process.pid)
        with runners._LIVE_LOCK:
            runners._LIVE.add(process)  # a force quit takes services with it
        self.live[service.id] = _Live(service.id, session.id, process.pid, session.process_created,
                                      at, service.stop_timeout_seconds, process, pump)
        self._snapshot(self.live[service.id])
        db.commit()
        self._set_state(db, service, ServiceState.running, None, next_attempt_at=None)

    def _begin_stop(self, live: _Live, reason: ServiceStopReason, at: datetime) -> None:
        live.stop_reason = reason
        live.kill_at = at + timedelta(seconds=live.stop_timeout)
        proc.ask_to_stop(live.pid)

    def _progress_stop(self, live: _Live, at: datetime) -> None:
        if live.stop_reason is not None and not live.killed and live.kill_at and at >= live.kill_at:
            proc.kill(live.pid, live.extra.get("descendants", ()))
            live.killed = True

    def _finished(self, live: _Live) -> bool:
        if live.process is not None:
            if live.process.poll() is None:
                return False
        elif proc.same_process(live.pid, live.created):  # adopted: known by pid and start time
            return False
        # The shell is gone; a program it started may not be. While a stop is
        # in progress that program gets its grace period; otherwise (an
        # unexpected exit) whatever is left is killed before any restart. The
        # same for an adopted process: read as gone the moment its shell died,
        # the model server under it was left running and a second copy started.
        if os.name != "nt" and proc.group_alive(live.pid):
            if live.stop_reason is not None and not live.killed:
                return False
            proc.kill(live.pid)
        if os.name == "nt":
            left = [(pid, started) for pid, started in live.extra.get("descendants", ())
                    if proc.same_process(pid, started)]
            if left:
                if live.stop_reason is not None and not live.killed:
                    return False
                proc.kill(live.pid, left)
        return True

    def _close_session(self, db: Session, live: _Live, at: datetime) -> int | None:
        proc.forget_usage(live.pid)
        db.execute(update(Service).where(Service.id == live.service_id).values(
            cpu_percent=None, rss_bytes=None, process_count=None, thread_count=None,
            resources_at=None, updated_at=Service.updated_at))
        code = live.process.returncode if live.process is not None else None
        if live.process is not None:
            with runners._LIVE_LOCK:
                runners._LIVE.discard(live.process)
            if live.pump is not None:
                live.pump.join(timeout=2)
                if live.pump.is_alive():
                    # A grandchild kept the pipe open; close our end so the
                    # pump thread, the pipe and the log handle do not live on.
                    try:
                        live.process.stdout.close()
                    except OSError:
                        pass
        reason = (ServiceStopReason.killed_after_timeout if live.killed else live.stop_reason)
        if reason is None:
            reason = ServiceStopReason.exited if code == 0 else ServiceStopReason.crashed
        session = db.get(ServiceSession, live.session_id)
        if session is not None:
            session.stopped_at, session.exit_code, session.stop_reason = at, code, reason
        return code

    def _finish(self, db: Session, service: Service, live: _Live, at: datetime) -> None:
        expected = live.stop_reason is not None
        code = self._close_session(db, live, at)
        del self.live[service.id]
        if live.stop_reason == ServiceStopReason.restarted:
            service.restart_requested = False
        db.commit()
        if expected:
            # A restart keeps the row in `stopping` (still holding its lock)
            # for the start that follows in the same pass.
            if live.stop_reason != ServiceStopReason.restarted:
                self._set_state(db, service, ServiceState.stopped, None)
            return
        self._after_unexpected_exit(db, service, at, code,
                                    f"Exited with code {code}" if code is not None else "Exited")

    def _after_unexpected_exit(self, db: Session, service: Service, at: datetime,
                               code: int | None, what: str) -> None:
        """Restart after a backoff, hold it stopped, or call it a crash loop."""
        policy = service.restart_policy
        restart = policy == RestartPolicy.always or (
            policy == RestartPolicy.on_failure and code != 0)
        if not restart:
            set_override(service, False, "exited", at, keep=True)
            db.commit()
            self._set_state(db, service, ServiceState.stopped,
                            f"{what}; not restarted ({policy.value.replace('_', ' ')})")
            return
        # Only the attempts since a person last acted count: their Start is a
        # fresh run of tries, not the sixth exit of the loop they just cleared.
        since = _aware(service.attempts_from)
        recent = [s for s in db.scalars(
            select(ServiceSession).where(ServiceSession.service_id == service.id)
            .order_by(ServiceSession.id.desc()).limit(CRASH_LOOP_EXITS * 2))
            if since is None or _aware(s.started_at) > since]
        in_window = [s for s in recent if s.stop_reason in _UNEXPECTED
                     and s.stopped_at is not None and _aware(s.stopped_at) >= at - CRASH_LOOP_WINDOW]
        if len(in_window) >= CRASH_LOOP_EXITS:
            set_override(service, False, "crash_loop", at, keep=True)
            detail = (f"Crash loop: exited {len(in_window)} times in "
                      f"{int(CRASH_LOOP_WINDOW.total_seconds() // 60)} minutes · {what}")
            db.commit()
            self._set_state(db, service, ServiceState.crash_loop, detail, next_attempt_at=None)
            self._notify(service, f"Service '{service.name}' stopped: crash loop. {what}.")
            return
        consecutive = 0
        for session in recent:
            ran = (_aware(session.stopped_at) - _aware(session.started_at)
                   if session.stopped_at else timedelta(0))
            if session.stop_reason not in _UNEXPECTED or ran >= STABLE_AFTER:
                break
            consecutive += 1
        delay = min(MAX_BACKOFF_SECONDS, 2 ** max(0, consecutive - 1))
        self._set_state(db, service, ServiceState.backoff, f"{what} · restarting in {delay}s",
                        next_attempt_at=at + timedelta(seconds=delay))

    def _sample(self, db: Session, service: Service, live: _Live, at: datetime) -> None:
        """Record what the process tree is using, every few seconds."""
        last = live.extra.get("sampled_at")
        if last is not None and (at - last).total_seconds() < RESOURCE_SAMPLE_SECONDS:
            return
        live.extra["sampled_at"] = at
        reading = proc.usage(live.pid)
        if reading is None:
            return
        db.execute(update(Service).where(Service.id == service.id).values(
            cpu_percent=reading.cpu_percent, rss_bytes=reading.rss_bytes,
            process_count=reading.processes, thread_count=reading.threads,
            resources_at=at, updated_at=Service.updated_at))
        db.commit()

    # ── bookkeeping ────────────────────────────────────────────────────────
    def _set_state(self, db: Session, service: Service, state: ServiceState, detail: str | None,
                   next_attempt_at: datetime | None | type = ...) -> None:
        changed = service.state != state or service.state_detail != detail
        if state != service.state:
            service.state_since = self.clock()
        service.state, service.state_detail = state, detail
        if next_attempt_at is not ...:
            changed = changed or _aware(service.next_attempt_at) != next_attempt_at
            service.next_attempt_at = next_attempt_at
        db.commit()
        if changed:
            _ws_manager.notify({"type": "service_updated", "id": service.id})

    def _idle_detail(self, service: Service, at: datetime) -> str | None:
        if service.override_running is False and service.override_reason == "exited":
            return service.state_detail
        if service.start_mode == ServiceStartMode.schedule:
            if not service.windows_json:
                return "No windows set"
            if not is_active(service.windows_json, service.timezone, at):
                return "Outside its windows"
        return None  # stopped by hand

    def _notify(self, service: Service, text: str) -> None:
        url = service.notify_webhook_url or runtime_settings.current("notify_webhook_url")
        if url:
            notify._post(url, text, event="service_crash_loop", service=service.name)

    def _lease(self, db: Session, at: datetime) -> bool:
        if self.holding and self._renewed and at - self._renewed < LEASE_RENEW_EVERY:
            return True
        renewed = db.execute(update(SupervisorLease).where(
            SupervisorLease.name == LEASE_NAME,
            (SupervisorLease.owner == self.owner)
            | (SupervisorLease.heartbeat_at < at - LEASE_STALE_AFTER),
        ).values(owner=self.owner, heartbeat_at=at)).rowcount
        if renewed:
            db.commit()
            self._renewed = at
            return True
        if db.get(SupervisorLease, LEASE_NAME) is not None:
            db.rollback()
            return False
        db.add(SupervisorLease(name=LEASE_NAME, owner=self.owner, heartbeat_at=at))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        self._renewed = at
        return True

    def _take_over(self, db: Session, at: datetime) -> None:
        """Deal with whatever a previous supervisor left: running processes
        are stopped (they write to a pipe nobody reads, and their state is
        unknown) and their services start fresh; dead ones are closed."""
        open_sessions = db.scalars(select(ServiceSession).where(
            ServiceSession.stopped_at.is_(None))).all()
        for session in open_sessions:
            if session.service_id in self.live:
                continue
            service = db.get(Service, session.service_id)
            if service is not None and proc.same_process(session.pid, session.process_created):
                live = _Live(service.id, session.id, session.pid, session.process_created,
                             _aware(session.started_at) or at, service.stop_timeout_seconds)
                self.live[service.id] = live
                self._snapshot(live)
                self._begin_stop(live, ServiceStopReason.interrupted, at)
            else:
                session.stopped_at, session.stop_reason = at, ServiceStopReason.interrupted
        for service in db.scalars(select(Service).where(
                Service.state.in_((*HOLDING, ServiceState.waiting_lock, ServiceState.backoff)))):
            if service.id not in self.live:
                service.state, service.state_detail, service.state_since = (
                    ServiceState.stopped, None, at)
        db.commit()


def log_tail(path: str | None, max_bytes: int) -> str:
    """The last `max_bytes` of a session's log, reaching back through the
    rotated files when the current one is short: right after a rotation it
    holds a few lines and the previous ten megabytes sit in `.1`."""
    if not path or not confined(path, get_settings().logs_dir):
        return ""
    file = Path(path)
    parts: list[bytes] = []
    remaining = max_bytes
    for candidate in (file, *(file.with_name(f"{file.name}.{n}")
                              for n in range(1, proc.LOG_BACKUPS + 1))):
        if remaining <= 0:
            break
        try:
            with candidate.open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - remaining))
                data = handle.read()
        except OSError:
            break
        parts.insert(0, data)
        remaining -= len(data)
    data = b"".join(parts)
    text = data.decode("utf-8", errors="replace")
    if len(data) >= max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text
