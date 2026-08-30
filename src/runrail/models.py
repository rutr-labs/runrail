import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
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
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.queued, index=True)
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
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
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
    # Free text: RunRail has no accounts, so this is attribution, not identity.
    approved_by: Mapped[str | None] = mapped_column(String(120))
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
    # Optional, never validated, never an identity claim — there is no auth.
    author: Mapped[str | None] = mapped_column(String(80))
    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="notes")


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_run_id: Mapped[int | None] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType), default=ArtifactType.other)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
