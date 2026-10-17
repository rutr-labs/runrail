"""services: long-running processes the supervisor keeps up

Three tables: services (configuration, desired-state override, and the state
the supervisor last wrote), service_sessions (one row per life of a process),
and supervisor_leases (so exactly one process supervises). Enum-like columns
are plain strings, so a later value needs no ALTER TYPE on PostgreSQL.
"""

import sqlalchemy as sa
from alembic import op

revision = "q9e2c4a7b5d3"
down_revision = "p4d8b1f6e3a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "services",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("key", sa.String(32), nullable=False, unique=True),
        sa.Column("description", sa.Text),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("cwd", sa.Text),
        sa.Column("project_id", sa.Integer,
                  sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("environment_id", sa.Integer,
                  sa.ForeignKey("environments.id", ondelete="SET NULL")),
        sa.Column("env_vars_json", sa.JSON),
        sa.Column("start_mode", sa.String(32), nullable=False),
        sa.Column("windows_json", sa.JSON),
        sa.Column("timezone", sa.String(64)),
        sa.Column("restart_policy", sa.String(32), nullable=False),
        sa.Column("stop_timeout_seconds", sa.Integer, nullable=False),
        sa.Column("lock_resource", sa.String(255)),
        sa.Column("lock_mode", sa.String(32), nullable=False),
        sa.Column("notify_webhook_url", sa.Text),
        sa.Column("override_running", sa.Boolean),
        sa.Column("override_until", sa.DateTime(timezone=True)),
        sa.Column("override_reason", sa.String(32)),
        sa.Column("restart_requested", sa.Boolean, nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("state_detail", sa.Text),
        sa.Column("state_since", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "service_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("service_id", sa.Integer,
                  sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("pid", sa.Integer),
        sa.Column("process_created", sa.Float),
        sa.Column("exit_code", sa.Integer),
        sa.Column("stop_reason", sa.String(32)),
        sa.Column("command", sa.Text),
        sa.Column("log_path", sa.Text),
        sa.Column("owner", sa.String(128)),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_service_sessions_service_id_started_at", "service_sessions",
                    ["service_id", "started_at"])
    op.create_table(
        "supervisor_leases",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("supervisor_leases")
    op.drop_index("ix_service_sessions_service_id_started_at", "service_sessions")
    op.drop_table("service_sessions")
    op.drop_table("services")
