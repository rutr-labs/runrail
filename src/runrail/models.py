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


class TaskRunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, default=1)
    notify_webhook_url: Mapped[str | None] = mapped_column(Text)
    auto_pause_failures: Mapped[int | None] = mapped_column(Integer)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    default_environment_id: Mapped[int | None] = mapped_column(ForeignKey("environments.id", ondelete="SET NULL"))
    tasks: Mapped[list["Task"]] = relationship(cascade="all, delete-orphan", back_populates="workflow")


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
    workflow: Mapped[Workflow] = relationship()
    task_runs: Mapped[list["TaskRun"]] = relationship(cascade="all, delete-orphan", back_populates="workflow_run")


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
    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="task_runs")
    task: Mapped[Task] = relationship()

    @property
    def task_name(self) -> str | None:
        return self.task.name if self.task else None

    @property
    def task_type(self) -> str | None:
        return self.task.task_type.value if self.task else None


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_run_id: Mapped[int | None] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    workflow_run_id: Mapped[int | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType), default=ArtifactType.other)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
