import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from runrail.api.ws import manager as _ws_manager
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.maintenance import cleanup_runs
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun


def enqueue_scheduled(workflow_id: int) -> None:
    with SessionLocal() as db:
        workflow = db.get(Workflow, workflow_id)
        if not workflow or not workflow.enabled: return
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


class SchedulerService:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="UTC")

    #: Internal job ids that the workflow reconciliation in sync() must never remove.
    _INTERNAL_JOBS = frozenset({"sync", "cleanup"})

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
                # unset). APScheduler owns the DST semantics: a time skipped by a
                # spring-forward gap is not fired; a repeated fall-back time fires
                # once. pytz's UnknownTimeZoneError subclasses KeyError, hence the
                # broad except alongside ValueError for bad crontabs.
                tz = workflow.schedule_timezone or "UTC"
                trigger = CronTrigger.from_crontab(workflow.schedule_cron, timezone=tz)
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
