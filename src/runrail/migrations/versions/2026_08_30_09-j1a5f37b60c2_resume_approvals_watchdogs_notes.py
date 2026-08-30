"""resume, approval gates, snooze, schedule watchdogs, and run notes"""

import sqlalchemy as sa
from alembic import op

revision = "j1a5f37b60c2"
down_revision = "i9c4d82e5b17"
branch_labels = None
depends_on = None

_NEW_ENUM_VALUES = (
    ("runstatus", "waiting_approval"),
    ("taskrunstatus", "awaiting_approval"),
    ("taskrunstatus", "approved"),
    ("taskrunstatus", "rejected"),
)


def upgrade():
    # Resume
    op.add_column("workflow_runs",
                  sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("task_runs",
                  sa.Column("resume_index", sa.Integer(), nullable=False, server_default="0"))
    # Approval gates
    op.add_column("tasks", sa.Column("requires_approval", sa.Boolean(),
                                     nullable=False, server_default=sa.false()))
    op.add_column("tasks", sa.Column("approval_prompt", sa.Text(), nullable=True))
    op.add_column("task_runs", sa.Column("approved_by", sa.String(length=120), nullable=True))
    op.add_column("task_runs", sa.Column("approval_note", sa.Text(), nullable=True))
    op.add_column("task_runs", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    # Snooze
    op.add_column("workflows", sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflows", sa.Column("snooze_pauses_runs", sa.Boolean(),
                                         nullable=False, server_default=sa.false()))
    # Watchdogs
    op.add_column("workflows", sa.Column("missed_run_grace_minutes", sa.Integer(), nullable=True))
    op.add_column("workflows",
                  sa.Column("missed_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflows", sa.Column("sla_minutes", sa.Integer(), nullable=True))
    op.add_column("workflow_runs",
                  sa.Column("sla_breached_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "run_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_run_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_notes_workflow_run_id"), "run_notes", ["workflow_run_id"])
    # artifacts.workflow_run_id has carried no index since the initial schema;
    # the per-run artifact list and the latest-report lookup both filter on it.
    op.create_index(op.f("ix_artifacts_workflow_run_id"), "artifacts", ["workflow_run_id"])

    # New enum labels are DDL only on PostgreSQL, where sa.Enum compiles to a
    # native type. On SQLite it compiles to a bare VARCHAR(n) with no CHECK
    # constraint (create_constraint has defaulted to False since SQLAlchemy 1.4)
    # and SQLite does not enforce VARCHAR length — which is exactly why the whole
    # test suite can pass while PostgreSQL breaks.
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction on PostgreSQL < 12.
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for type_name, value in _NEW_ENUM_VALUES:
                op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade():
    op.drop_index(op.f("ix_artifacts_workflow_run_id"), table_name="artifacts")
    op.drop_index(op.f("ix_run_notes_workflow_run_id"), table_name="run_notes")
    op.drop_table("run_notes")
    op.drop_column("workflow_runs", "sla_breached_at")
    op.drop_column("workflows", "sla_minutes")
    op.drop_column("workflows", "missed_notified_at")
    op.drop_column("workflows", "missed_run_grace_minutes")
    op.drop_column("workflows", "snooze_pauses_runs")
    op.drop_column("workflows", "snooze_until")
    op.drop_column("task_runs", "approved_at")
    op.drop_column("task_runs", "approval_note")
    op.drop_column("task_runs", "approved_by")
    op.drop_column("tasks", "approval_prompt")
    op.drop_column("tasks", "requires_approval")
    op.drop_column("task_runs", "resume_index")
    op.drop_column("workflow_runs", "resume_count")
    # Enum values are deliberately not removed: PostgreSQL cannot drop a value
    # from an enum type, and rows may still reference them.
