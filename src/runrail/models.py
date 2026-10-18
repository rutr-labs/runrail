import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from runrail.db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops the offset on write; stored values are UTC.

    Every Python-side comparison against now() must route through this — a raw
    `stored > now()` raises TypeError on the default SQLite deployment.
    """
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class EnvironmentType(enum.StrEnum):
    system = "system"
    python = "python"
    conda = "conda"
    docker_placeholder = "docker_placeholder"


class EnvironmentStatus(enum.StrEnum):
    creating = "creating"
    building = "building"
    ready = "ready"
    degraded = "degraded"
    failed = "failed"


class TaskType(enum.StrEnum):
    shell = "shell"
    python = "python"
    notebook = "notebook"
    sql = "sql"


class LockMode(enum.StrEnum):
    # Only meaningful alongside a Workflow.lock_resource; inert without one.
    shared = "shared"
    exclusive = "exclusive"


class RunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"
    # New values are appended, never inserted: PostgreSQL's ALTER TYPE ADD VALUE
    # appends, and a migrated database must order its labels like a fresh one.
    # A *rejected* run lands cancelled — every existing consumer (notification
    # transitions, retention, daily stats) already treats that correctly.
    waiting_approval = "waiting_approval"


class TaskRunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"
    # An approval gate is its own TaskRun (attempt 0, no logs) and never lands
    # success — otherwise a resume would see the gated task as already
    # satisfied and the approved work would never execute.
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rejected = "rejected"


class TriggerType(enum.StrEnum):
    manual = "manual"
    schedule = "schedule"
    cli = "cli"
    backfill = "backfill"


class ArtifactType(enum.StrEnum):
    file = "file"
    notebook = "notebook"
    html = "html"
    log = "log"
    other = "other"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    root_path: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    default_environment_id: Mapped[int | None] = mapped_column(ForeignKey("environments.id", ondelete="SET NULL"))


class Environment(TimestampMixin, Base):
    __tablename__ = "environments"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    env_type: Mapped[EnvironmentType] = mapped_column(Enum(EnvironmentType), default=EnvironmentType.system)
    executable: Mapped[str | None] = mapped_column(Text)
    conda_env: Mapped[str | None] = mapped_column(String(255))
    env_vars_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    description: Mapped[str | None] = mapped_column(Text)
    managed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[EnvironmentStatus] = mapped_column(
        Enum(EnvironmentStatus), default=EnvironmentStatus.ready
    )
    base_executable: Mapped[str | None] = mapped_column(Text)
    packages_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    active_packages_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    python_version: Mapped[str | None] = mapped_column(String(100))
    build_log: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    build_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Workflow(TimestampMixin, Base):
    __tablename__ = "workflows"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    #: Identity for workflow-as-code: written into every YAML export and matched
    #: before the name on apply, so a file applied after a rename updates the
    #: renamed workflow instead of creating a duplicate that fires alongside it.
    key: Mapped[str] = mapped_column(String(32), unique=True,
                                     default=lambda: uuid.uuid4().hex)
    description: Mapped[str | None] = mapped_column(Text)
    schedule_cron: Mapped[str | None] = mapped_column(String(255))
    # IANA name (e.g. "Asia/Dubai"); NULL evaluates the cron in UTC as before.
    schedule_timezone: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, default=1)
    notify_webhook_url: Mapped[str | None] = mapped_column(Text)
    auto_pause_failures: Mapped[int | None] = mapped_column(Integer)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    default_environment_id: Mapped[int | None] = mapped_column(ForeignKey("environments.id", ondelete="SET NULL"))
    # Operator state, never exported and never part of WorkflowIn: mutes every
    # alert source until it expires by the clock.
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Distinct from `enabled`: `enabled` is configuration and gates job
    # registration; this gates enqueue_scheduled only and auto-expires.
    snooze_pauses_runs: Mapped[bool] = mapped_column(Boolean, default=False)
    # Configuration: minutes past an expected cron fire before the dead man's
    # switch alerts. NULL disables the watchdog for this workflow.
    missed_run_grace_minutes: Mapped[int | None] = mapped_column(Integer)
    # Operator state written only by the watchdog — one writer, one transition.
    missed_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Configuration: minutes from run creation to the promised finish.
    sla_minutes: Mapped[int | None] = mapped_column(Integer)
    # Mutual exclusion across workflows on a named resource (a database, a
    # licence, a mount). NULL is no locking — the behaviour every existing
    # workflow keeps. Exactly ONE resource per workflow: a run acquires all it
    # needs in the single UPDATE that claims it, so there is no partial hold to
    # wait on and deadlock is structurally impossible.
    lock_resource: Mapped[str | None] = mapped_column(String(255))
    lock_mode: Mapped[LockMode] = mapped_column(Enum(LockMode), default=LockMode.shared)
    # Alert state, written only by notify.py under a compare-and-set: "ok" or
    # "failing", and the run that put it there. A failure alerts when it moves
    # the state to failing, a success when it moves it back — whichever run
    # finishes first, resumed or not. Reconstructing that from run ids after
    # the fact lost recoveries on resume and double-sent on concurrent runs.
    alert_state: Mapped[str | None] = mapped_column(String(16))
    alert_run_id: Mapped[int | None] = mapped_column(Integer)
    # When the workflow was last switched on. Auto-pause counts failures from
    # here, so a workflow re-enabled after a pause is not paused again by the
    # very next failure on the strength of the streak that paused it before.
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set by the auto-pause that switched the workflow off, cleared when it is
    # switched on again: the feed's record of the pause.
    auto_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_paused_run_id: Mapped[int | None] = mapped_column(Integer)
    # The last webhook delivery failure, so a revoked or mistyped URL is
    # visible somewhere instead of failing silently forever.
    webhook_error: Mapped[str | None] = mapped_column(Text)
    webhook_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tasks: Mapped[list["Task"]] = relationship(cascade="all, delete-orphan", back_populates="workflow")

    @property
    def snoozed(self) -> bool:
        until = _aware(self.snooze_until)
        return until is not None and until > now()


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("workflow_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    environment_id: Mapped[int | None] = mapped_column(ForeignKey("environments.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255))
    task_type: Mapped[TaskType] = mapped_column(Enum(TaskType))
    command: Mapped[str | None] = mapped_column(Text)
    script_path: Mapped[str | None] = mapped_column(Text)
    notebook_path: Mapped[str | None] = mapped_column(Text)
    sql_path: Mapped[str | None] = mapped_column(Text)
    cwd: Mapped[str | None] = mapped_column(Text)
    depends_on_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    parameters_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    retry_delay_seconds: Mapped[int] = mapped_column(Integer, default=60)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_prompt: Mapped[str | None] = mapped_column(Text)
    workflow: Mapped[Workflow | None] = relationship(back_populates="tasks")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    # Composite, not two single-column indexes: every filtered run list is also
    # ordered newest-first, and a leading-column-only index makes the database
    # read and sort a workflow's entire history to return one page of it.
    __table_args__ = (
        Index("ix_workflow_runs_workflow_id_created_at", "workflow_id", "created_at"),
        Index("ix_workflow_runs_status_created_at", "status", "created_at"),
        # Read on every poll of the activity feed, where the predicate is
        # IS NOT NULL and nearly every row is NULL; without it that poll costs a
        # full table scan of every run ever.
        Index("ix_workflow_runs_sla_breached_at", "sla_breached_at"),
        # Ids are permalinks: never hand one out again after retention deletes it.
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.queued)
    trigger_type: Mapped[TriggerType] = mapped_column(Enum(TriggerType))
    run_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    parameters_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    # Counts HUMAN resumes only — never approval re-entry. A gate's wait is a
    # gap inside a segment, not a new one, so open_gate stamps the current
    # value onto its TaskRun.resume_index without bumping it.
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    # When the latest resume began. The SLA deadline is measured from here
    # rather than created_at once a run has been resumed: yesterday's run
    # resumed today is not twenty-three hours late the moment it starts.
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which alert this run's outcome sent, if any: "run_failed" or
    # "run_recovered". Stamped by notify.py when its transition wins.
    alert_kind: Mapped[str | None] = mapped_column(String(32))
    # Set once when the run passes its workflow's SLA; the marker itself is the
    # repeat guard, and resume must clear it so a second breach can re-alert.
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow: Mapped[Workflow] = relationship()
    task_runs: Mapped[list["TaskRun"]] = relationship(cascade="all, delete-orphan", back_populates="workflow_run")
    notes: Mapped[list["RunNote"]] = relationship(
        cascade="all, delete-orphan", back_populates="workflow_run",
        order_by="RunNote.created_at")


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = {"sqlite_autoincrement": True}  # see WorkflowRun
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    # Indexed for the cascade, not for a read: deleting one task made SQLite
    # scan every task run ever recorded to find the children to delete.
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    status: Mapped[TaskRunStatus] = mapped_column(Enum(TaskRunStatus), default=TaskRunStatus.queued)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None]
    exit_code: Mapped[int | None]
    stdout_log_path: Mapped[str | None] = mapped_column(Text)
    stderr_log_path: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    rendered_command: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Which resume segment of the run this row belongs to; rows from earlier
    # segments are what "reused" means.
    resume_index: Mapped[int] = mapped_column(Integer, default=0)
    approval_note: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="task_runs")
    task: Mapped[Task] = relationship()

    @property
    def task_name(self) -> str | None:
        return self.task.name if self.task else None

    @property
    def task_type(self) -> str | None:
        return self.task.task_type.value if self.task else None


class RunNote(TimestampMixin, Base):
    """Append-only annotations on a run. Deliberately a list, not one editable
    field: the real shape of the data is a thread, and the second annotation on
    an incident must not destroy the first. Cascades away with the run, so
    retention needs no change."""

    __tablename__ = "run_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="notes")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = {"sqlite_autoincrement": True}  # see WorkflowRun
    id: Mapped[int] = mapped_column(primary_key=True)
    # Same cascade reason as TaskRun.task_id: retention deletes runs, each
    # deleted task run then swept the whole artifacts table for children.
    task_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), index=True)
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType), default=ArtifactType.other)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# ─── Services ─────────────────────────────────────────────────────────────
# Long-running processes RunRail keeps up — a model server, a folder sync —
# rather than runs it executes to completion. Separate from workflows on
# purpose: a run is about finishing (an outcome, a duration, retries, SLAs),
# and a process meant to stay up would sit "running" forever, skew every
# duration statistic and hold a worker slot for good. The supervisor in
# runrail/services/supervisor.py owns their processes.
#
# Enums here are stored as plain strings (native_enum=False): they are new,
# and growing a native PostgreSQL enum needs its own migration per value.

class ServiceStartMode(enum.StrEnum):
    startup = "startup"    # kept running whenever RunRail is
    schedule = "schedule"  # running inside its weekly windows
    manual = "manual"      # running only once someone presses Start


class RestartPolicy(enum.StrEnum):
    always = "always"          # any exit while it should be running is restarted
    on_failure = "on_failure"  # only a non-zero exit is
    never = "never"


class ServiceState(enum.StrEnum):
    stopped = "stopped"
    waiting_lock = "waiting_lock"  # should run; its resource is held elsewhere
    starting = "starting"          # lock taken, process being launched
    running = "running"
    stopping = "stopping"          # asked to stop; force-killed at the timeout
    backoff = "backoff"            # exited unexpectedly; restarting after a delay
    crash_loop = "crash_loop"      # kept exiting; held stopped until a person acts


class ServiceStopReason(enum.StrEnum):
    stopped_by_user = "stopped_by_user"
    window_closed = "window_closed"
    restarted = "restarted"
    exited = "exited"                      # on its own, exit code 0
    crashed = "crashed"                    # on its own, non-zero
    killed_after_timeout = "killed_after_timeout"
    failed_to_start = "failed_to_start"
    runrail_shutdown = "runrail_shutdown"
    interrupted = "interrupted"            # RunRail died; found afterwards
    deleted = "deleted"


def _str_enum(cls):
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class Service(TimestampMixin, Base):
    __tablename__ = "services"
    # The supervisor keeps processes by service id: a recycled id would hand a
    # new service the old one's process.
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    #: YAML identity, as Workflow.key: apply survives a rename.
    key: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: uuid.uuid4().hex)
    description: Mapped[str | None] = mapped_column(Text)
    command: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL"))
    env_vars_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    start_mode: Mapped[ServiceStartMode] = mapped_column(
        _str_enum(ServiceStartMode), default=ServiceStartMode.manual)
    #: [{"days": ["mon", ...], "start": "HH:MM", "end": "HH:MM"}] — see services/windows.py.
    windows_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    timezone: Mapped[str | None] = mapped_column(String(64))
    restart_policy: Mapped[RestartPolicy] = mapped_column(
        _str_enum(RestartPolicy), default=RestartPolicy.always)
    #: Grace between the polite stop and the forced kill.
    stop_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    lock_resource: Mapped[str | None] = mapped_column(String(255))
    lock_mode: Mapped[LockMode] = mapped_column(_str_enum(LockMode), default=LockMode.exclusive)
    notify_webhook_url: Mapped[str | None] = mapped_column(Text)

    # Desired-state override: a Start/Stop click, or a hold the supervisor
    # placed itself (a clean exit under on_failure, a crash loop). NULL is "no
    # override"; override_until NULL is "until changed again".
    override_running: Mapped[bool | None] = mapped_column(Boolean)
    override_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    override_reason: Mapped[str | None] = mapped_column(String(32))
    restart_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Exits before this instant do not count toward a crash loop: set when a
    #: person clears a hold, so their Start gets a fresh run of attempts.
    attempts_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # What the process tree is using, sampled by the supervisor every few
    # seconds while the service runs: the first thing you want to know about a
    # model server. Cleared when it stops.
    cpu_percent: Mapped[float | None]  # per core, as top reports it
    rss_bytes: Mapped[int | None] = mapped_column(BigInteger)
    process_count: Mapped[int | None]
    thread_count: Mapped[int | None]
    resources_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Written by the supervisor, read by the API and the run claim (locks).
    state: Mapped[ServiceState] = mapped_column(_str_enum(ServiceState),
                                                default=ServiceState.stopped)
    state_detail: Mapped[str | None] = mapped_column(Text)
    state_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list["ServiceSession"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, back_populates="service")


class AppSetting(Base):
    """One setting saved from the Settings page; see runrail.runtime_settings."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ServiceSession(Base):
    """One life of a service's process, start to exit."""

    __tablename__ = "service_sessions"
    __table_args__ = (
        Index("ix_service_sessions_service_id_started_at", "service_id", "started_at"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pid: Mapped[int | None]
    #: psutil create_time of `pid`: a recorded pid is only ever signalled while
    #: it still names the same process, never one that reused the number.
    process_created: Mapped[float | None]
    exit_code: Mapped[int | None]
    stop_reason: Mapped[ServiceStopReason | None] = mapped_column(_str_enum(ServiceStopReason))
    command: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(Text)
    #: The supervisor that started it.
    owner: Mapped[str | None] = mapped_column(String(128))
    service: Mapped[Service] = relationship(back_populates="sessions")


class SupervisorLease(Base):
    """Exactly one process supervises services: `serve` and a separate `worker`
    may both be running, and two supervisors would each start a copy."""

    __tablename__ = "supervisor_leases"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
