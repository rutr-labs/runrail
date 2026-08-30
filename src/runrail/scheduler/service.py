import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runrail import notify
from runrail.api.ws import manager as _ws_manager
from runrail.config import get_settings
from runrail.crontab import cron_trigger
from runrail.db import SessionLocal
from runrail.maintenance import cleanup_runs
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun, _aware, now


def enqueue_scheduled(workflow_id: int) -> None:
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        if not workflow or not workflow.enabled: return
        # Gated here rather than in sync(): the job keeps firing and no-ops, so
        # expiry needs no watcher, no cleanup pass and no state machine — the
        # first fire after snooze_until simply enqueues again.
        if workflow.snoozed and workflow.snooze_pauses_runs: return
        # Coalesce instead of skip: while a run is executing, keep exactly one queued
        # iteration waiting. The worker enforces max_concurrent_runs when claiming, so
        # a slow run delays — never silently drops — the next scheduled iteration.
        queued = db.scalar(select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status == RunStatus.queued)) or 0
        if queued >= 1: return
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        key = f"schedule:{workflow_id}:{minute.isoformat()}"
        if db.scalar(select(WorkflowRun).where(WorkflowRun.run_key == key)): return
        # Don't persist `ds` as an explicit run parameter — it would show up as a
        # parameter on every scheduled run even when the workflow never templates
        # it. The worker still defaults `ds` to the run's date for `{{ ds }}`
        # rendering (see _context in worker/service.py); only backfills, which are
        # inherently date-driven, set `ds` deliberately.
        run = WorkflowRun(workflow_id=workflow_id, status=RunStatus.queued,
                          trigger_type=TriggerType.schedule, run_key=key)
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # another scheduler process enqueued this minute first
            return
        # No-op when the scheduler runs as a separate process without the API loop.
        _ws_manager.notify({"type": "run_created", "id": run.id, "workflow_id": workflow_id})


#: A workflow in any of these states is not silent, it is busy. enqueue_scheduled
#: deliberately drops a fire while an iteration is already queued, and a gated run
#: is waiting on a human — neither means the schedule died. Busy is not the same
#: as healthy: check_sla_breaches reads the same set and reports the oldest of
#: these runs once it is past its deadline, the gated one included.
_IN_FLIGHT = (RunStatus.queued, RunStatus.running, RunStatus.waiting_approval)

#: Floor for the optional anchor terms. updated_at is always present, so it never wins alone.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _expected_fire(workflow: Workflow, anchor: datetime) -> datetime | None:
    """The first firing the schedule owes after `anchor`.

    APScheduler's own trigger, never a second cron implementation: a watchdog
    that disagreed with the scheduler about a DST boundary would alert at 2am
    about a fire that correctly never happened.
    """
    try:
        trigger = cron_trigger(workflow.schedule_cron, workflow.schedule_timezone or "UTC")
    except (ValueError, KeyError):
        return None  # a crontab sync() also rejects: skipped, never raised
    return trigger.get_next_fire_time(None, anchor)


def _mark_missed(db: Session, workflow: Workflow, value: datetime | None) -> bool:
    """Guarded transition on missed_notified_at; True when this process won it.

    Two scheduler processes must not both post, hence the WHERE guard and
    rowcount rather than read-then-write. updated_at is pinned to itself because
    it anchors the check — letting onupdate bump it would make the watchdog's own
    write look like an operator edit and push the next expected fire forward.
    """
    guard = (Workflow.missed_notified_at.is_(None) if value is not None
             else Workflow.missed_notified_at.is_not(None))
    changed = db.execute(
        update(Workflow).where(Workflow.id == workflow.id, guard)
        .values(missed_notified_at=value, updated_at=Workflow.updated_at)).rowcount
    db.commit()
    return bool(changed)


def check_missed_runs(db: Session) -> None:
    """Alert once when a schedule goes silent and once when it comes back.

    Opt-in per workflow through missed_run_grace_minutes (NULL = off), so an
    upgrade never starts posting about workflows nobody asked to be watched.
    `enabled` is deliberately not a precondition: "someone paused it and forgot"
    is the likeliest cause of a dead pipeline, so the alert names it instead.
    """
    workflows = db.scalars(select(Workflow).where(
        Workflow.schedule_cron.is_not(None),
        Workflow.missed_run_grace_minutes.is_not(None))).all()
    if not workflows:
        return
    # Three queries regardless of workflow count — this runs every 60 seconds.
    ids = [workflow.id for workflow in workflows]
    last_run = dict(db.execute(
        select(WorkflowRun.workflow_id, func.max(WorkflowRun.created_at))
        .where(WorkflowRun.workflow_id.in_(ids))
        .group_by(WorkflowRun.workflow_id)).all())
    in_flight = set(db.scalars(select(WorkflowRun.workflow_id).where(
        WorkflowRun.workflow_id.in_(ids), WorkflowRun.status.in_(_IN_FLIGHT))))
    current = now()
    for workflow in workflows:
        # A mute must not even record the transition: a marker set while snoozed
        # would fire a spurious recovery the moment the snooze lifts.
        if workflow.snoozed:
            continue
        last = _aware(last_run.get(workflow.id))
        # The last moment the schedule is known to have been observed. Any run
        # counts — the operator's question is "did it run", not "did cron fire
        # it". snooze_until keeps fires skipped during a mute from counting.
        # updated_at covers re-enabling, cron edits and never-run workflows in
        # one term, so an unrelated edit delays detection by one interval: the
        # deliberate trade for zero extra columns. Do not "fix" it.
        anchor = max(_aware(workflow.updated_at) or _EPOCH,
                     _aware(workflow.snooze_until) or _EPOCH, last or _EPOCH)
        expected = _expected_fire(workflow, anchor)
        # "No run since the expected fire" needs no query: expected is by
        # construction later than every run this workflow has.
        missed = (expected is not None and workflow.id not in in_flight
                  and current - expected > timedelta(minutes=workflow.missed_run_grace_minutes))
        if missed:
            if _mark_missed(db, workflow, current):
                # Converted, not passed through: the trigger returns the fire in
                # the workflow's own timezone and the message labels it UTC, so a
                # Dubai schedule would otherwise report 09:00 for an 05:00 fire.
                notify.notify_missed_run(workflow, expected.astimezone(timezone.utc),
                                         f"{last:%Y-%m-%d %H:%M} UTC" if last else "never")
        # Recovery takes an actual run, not merely the absence of a miss: an
        # unrelated edit moves the anchor, and announcing a recovery for a
        # workflow that is still dead is worse than staying quiet.
        elif workflow.missed_notified_at is not None and (
                workflow.id in in_flight
                or (last is not None and last > _aware(workflow.missed_notified_at))):
            if _mark_missed(db, workflow, None):
                notify.notify_missed_run_recovered(workflow)


def check_sla_breaches(db: Session) -> None:
    """Alert once, while the run is still in flight, when it passes the deadline
    its workflow promised.

    Repeat avoidance is structural: the transition key is the run, so a marked
    run alerts once however long it then overruns. The next run starts clean with
    no reset logic, and retention deletes the marker with the run.
    """
    rows = db.execute(
        select(WorkflowRun, Workflow)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(Workflow.sla_minutes.is_not(None),
               # waiting_approval is in: a run parked on a human eight hours past
               # a thirty-minute deadline has missed that deadline, and the
               # operator who set sla_minutes asked to hear about exactly that.
               # Backfills are bulk work: a 30-day range would breach in one burst
               # as the queue drains.
               WorkflowRun.status.in_(_IN_FLIGHT),
               WorkflowRun.trigger_type != TriggerType.backfill)
        .order_by(WorkflowRun.workflow_id, WorkflowRun.created_at, WorkflowRun.id)).all()
    current = now()
    reported: set[int] = set()
    for run, workflow in rows:
        # Only the oldest in-flight run of a workflow can breach. Everything
        # behind it — above all the iteration enqueue_scheduled coalesced behind
        # an approval gate — is late BECAUSE of it, and alerting on the follower
        # names the one run that is blameless.
        if workflow.id in reported:
            continue
        reported.add(workflow.id)
        # A run already marked keeps its slot rather than handing the alert down
        # the queue on the next tick: that is one incident, not two. And no marker
        # while snoozed, or the late-finish message notify.py sends on success
        # leaks out after the mute lifts.
        if run.sla_breached_at is not None or workflow.snoozed:
            continue
        # From created_at, not started_at: for a scheduled run that is the cron
        # fire minute, so "240" on a 02:00 workflow means "done by 06:00" with no
        # timezone arithmetic — and it is the only origin that catches a run
        # which never started because the worker is dead.
        deadline = _aware(run.created_at) + timedelta(minutes=workflow.sla_minutes)
        if current < deadline:
            continue
        changed = db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run.id, WorkflowRun.sla_breached_at.is_(None))
            .values(sla_breached_at=current)).rowcount
        db.commit()
        if changed:
            notify.notify_sla_breach(workflow, run, deadline)


def check_watchdogs(db: Session) -> None:
    """Both schedule watchdogs in one pass, one session; snoozed workflows fire
    neither.

    Honest limitation: this runs inside the scheduler process, so it cannot
    report its own death. What it does catch is a host that slept or rebooted
    (APScheduler's misfire grace skips those fires, so the runs truly never
    happened), a workflow left disabled, a crontab sync() silently rejected, and
    a run that overran its deadline. Calling it from an external timer is the
    answer to a fully dead process.
    """
    check_missed_runs(db)
    check_sla_breaches(db)


class SchedulerService:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="UTC")

    #: Internal job ids that the workflow reconciliation in sync() must never remove.
    _INTERNAL_JOBS = frozenset({"sync", "cleanup", "watchdog"})

    def sync(self) -> None:
        with SessionLocal() as db:
            workflows = db.scalars(select(Workflow).where(
                Workflow.enabled.is_(True), Workflow.schedule_cron.is_not(None))).all()
        wanted = set(self._INTERNAL_JOBS)
        for workflow in workflows:
            if not workflow.schedule_cron: continue
            job_id = f"workflow-{workflow.id}"; wanted.add(job_id)
            try:
                # Cron fields are wall-clock in the workflow's timezone (UTC when
                # unset). APScheduler owns the DST semantics, and they are not the
                # intuitive ones: a wall time inside a spring-forward gap still
                # fires, resolved against the offset in force before the jump (so
                # 02:30 runs at 03:30 on that one day), and a repeated fall-back
                # time fires twice. ZoneInfo raises KeyError for an unknown zone,
                # hence the broad except alongside ValueError for bad crontabs.
                tz = workflow.schedule_timezone or "UTC"
                trigger = cron_trigger(workflow.schedule_cron, tz)
                # misfire_grace_time: APScheduler's default of 1 second silently skips
                # a firing the moment the process is briefly busy or the host wakes from
                # sleep. Late enqueueing is safe here — runs are deduped per minute and
                # coalesced to one queued iteration.
                self.scheduler.add_job(enqueue_scheduled, trigger, [workflow.id], id=job_id,
                                       replace_existing=True, coalesce=True, max_instances=1,
                                       misfire_grace_time=55)
            except (ValueError, KeyError):
                continue
        for job in self.scheduler.get_jobs():
            if job.id not in wanted: self.scheduler.remove_job(job.id)

    @staticmethod
    def _watchdog() -> None:
        with SessionLocal() as db:
            check_watchdogs(db)

    @staticmethod
    def _cleanup() -> None:
        retention_days = get_settings().retention_days
        if not retention_days:
            return
        with SessionLocal() as db:
            cleanup_runs(db, retention_days)

    def start(self) -> None:
        self.scheduler.start(); self.sync()
        self.scheduler.add_job(self.sync, "interval", seconds=30, id="sync",
                               replace_existing=True, coalesce=True, max_instances=1,
                               misfire_grace_time=25)
        # One 60s tick for both watchdogs: three queries each, and both compare
        # against tolerances measured in minutes, so a finer tick buys nothing.
        self.scheduler.add_job(self._watchdog, "interval", seconds=60, id="watchdog",
                               replace_existing=True, coalesce=True, max_instances=1,
                               misfire_grace_time=55)
        if get_settings().retention_days:
            self.scheduler.add_job(self._cleanup, "interval", hours=6, id="cleanup",
                                   replace_existing=True, coalesce=True, max_instances=1,
                                   misfire_grace_time=3600)

    def shutdown(self) -> None:
        if self.scheduler.running: self.scheduler.shutdown(wait=False)

    def run_forever(self) -> None:
        self.start()
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()
