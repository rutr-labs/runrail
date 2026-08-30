from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from runrail.models import EnvironmentStatus, EnvironmentType, LockMode, TaskType


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; tag them as UTC so API clients parse them correctly."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectIn(BaseModel):
    name: str
    root_path: str
    description: str | None = None
    default_environment_id: int | None = None


class ProjectOut(ProjectIn, ORMModel):
    id: int
    created_at: UTCDateTime
    updated_at: UTCDateTime


class EnvironmentIn(BaseModel):
    name: str
    env_type: EnvironmentType = EnvironmentType.python
    executable: str | None = None
    conda_env: str | None = None
    env_vars_json: dict[str, Any] | None = None
    description: str | None = None
    create_venv: bool = False
    packages: list[str] = Field(default_factory=list)
    base_executable: str | None = None


class EnvironmentRebuild(BaseModel):
    packages: list[str] | None = None
    base_executable: str | None = None


class EnvironmentUpdate(BaseModel):
    name: str | None = None
    env_type: EnvironmentType | None = None
    executable: str | None = None
    conda_env: str | None = None
    env_vars_json: dict[str, Any] | None = None
    description: str | None = None

    @field_validator("env_type", mode="before")
    @classmethod
    def empty_environment_type(cls, value):
        return None if value in (None, "", "null") else value


class EnvironmentOut(ORMModel):
    id: int
    name: str
    env_type: EnvironmentType
    executable: str | None
    conda_env: str | None
    env_vars_json: dict[str, Any] | None
    description: str | None
    managed: bool
    status: EnvironmentStatus
    base_executable: str | None
    packages_json: list[str]
    active_packages_json: list[str]
    python_version: str | None
    build_log: str | None
    last_error: str | None
    last_built_at: UTCDateTime | None
    build_started_at: UTCDateTime | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class WorkflowIn(BaseModel):
    name: str
    description: str | None = None
    schedule_cron: str | None = None
    schedule_timezone: str | None = None
    enabled: bool = True
    max_concurrent_runs: int = Field(default=1, ge=1)
    project_id: int | None = None
    default_environment_id: int | None = None
    notify_webhook_url: str | None = None
    auto_pause_failures: int | None = Field(default=None, ge=1)
    # Configuration only. Operator state (snooze_until, snooze_pauses_runs,
    # missed_notified_at, sla_breached_at) must never appear here: apply_update
    # writes every key of WorkflowIn.model_dump(), so an edit-modal save would
    # silently clear a snooze. Those live on dedicated endpoints instead.
    missed_run_grace_minutes: int | None = Field(default=None, ge=1)
    sla_minutes: int | None = Field(default=None, ge=1)
    # Configuration: the named resource this workflow serialises on. NULL is no
    # locking, and the mode is inert without a resource.
    lock_resource: str | None = Field(default=None, max_length=255)
    lock_mode: LockMode = LockMode.shared

    @model_validator(mode="after")
    def _mode_needs_a_resource(self) -> "WorkflowIn":
        # An empty box in the edit modal means "no lock", and a mode stored
        # without a resource is configuration that claims a rule it never applies.
        resource = (self.lock_resource or "").strip()
        self.lock_resource = resource or None
        if not resource:
            self.lock_mode = LockMode.shared
        return self

    @field_validator("schedule_timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except Exception as exc:  # ZoneInfoNotFoundError, ValueError on bad keys
            raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
        return value


class WorkflowOut(WorkflowIn, ORMModel):
    id: int
    created_at: UTCDateTime
    updated_at: UTCDateTime
    snooze_until: UTCDateTime | None = None
    snooze_pauses_runs: bool = False
    snoozed: bool = False  # from the model property, via from_attributes


class SnoozeIn(BaseModel):
    until: UTCDateTime
    pause_runs: bool = False

    @field_validator("until")
    @classmethod
    def _bounded_window(cls, value: datetime) -> datetime:
        # A fat-fingered year must not permanently mute a pipeline.
        current = datetime.now(timezone.utc)
        if value <= current:
            raise ValueError("Snooze must end in the future")
        if value > current + timedelta(days=30):
            raise ValueError("Snooze cannot exceed 30 days")
        return value


class TaskIn(BaseModel):
    name: str
    task_type: TaskType
    project_id: int | None = None
    environment_id: int | None = None
    command: str | None = None
    script_path: str | None = None
    notebook_path: str | None = None
    sql_path: str | None = None
    cwd: str | None = None
    depends_on_json: list[str] = Field(default_factory=list)
    parameters_json: dict[str, Any] | None = None
    retries: int = Field(default=0, ge=0)
    retry_delay_seconds: int = Field(default=60, ge=0)
    timeout_seconds: int | None = Field(default=None, ge=1)
    requires_approval: bool = False
    approval_prompt: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def check_required_source(self) -> "TaskIn":
        required = {
            TaskType.shell: ("command", self.command),
            TaskType.python: ("script_path or command", self.script_path or self.command),
            TaskType.notebook: ("notebook_path", self.notebook_path),
            TaskType.sql: ("sql_path", self.sql_path),
        }
        field, value = required[self.task_type]
        if not value:
            raise ValueError(f"A {self.task_type.value} task requires {field}")
        if self.name in self.depends_on_json:
            raise ValueError("A task cannot depend on itself")
        return self


class TaskOut(TaskIn, ORMModel):
    id: int
    workflow_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class RunCreate(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)


class BackfillCreate(BaseModel):
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")
    parameters: dict[str, Any] = Field(default_factory=dict)


class TaskRunOut(ORMModel):
    id: int
    workflow_run_id: int
    task_id: int
    task_name: str | None = None
    task_type: str | None = None
    status: str
    attempt: int
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    duration_seconds: float | None
    exit_code: int | None
    error_message: str | None
    rendered_command: str | None
    created_at: UTCDateTime
    resume_index: int = 0
    approval_note: str | None = None
    approved_at: UTCDateTime | None = None


class WorkflowRunOut(ORMModel):
    id: int
    workflow_id: int
    status: str
    trigger_type: str
    run_key: str | None
    parameters_json: dict[str, Any] | None
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    duration_seconds: float | None
    created_at: UTCDateTime
    resume_count: int = 0
    sla_breached_at: UTCDateTime | None = None


class ResumeIn(BaseModel):
    """Task names to force out of the reuse set; everything else is decided by
    the plan."""

    rerun: list[str] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    """Why the gate was decided. Optional: the decision itself is the payload."""

    note: str | None = Field(default=None, max_length=2000)


class RunNoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def _body_has_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A note needs a body")
        return value


class RunNoteOut(ORMModel):
    id: int
    workflow_run_id: int
    body: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class WorkflowRunDetail(WorkflowRunOut):
    task_runs: list[TaskRunOut] = Field(default_factory=list)
    # Lazy-loaded from the relationship while the request session is open; the
    # run page reads them here instead of making a second round trip.
    notes: list[RunNoteOut] = Field(default_factory=list)
