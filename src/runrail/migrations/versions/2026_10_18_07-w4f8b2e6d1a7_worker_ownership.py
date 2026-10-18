"""which worker is executing a run, and whether that worker is alive

Recovery failed every `running` run on startup, whoever was executing it, so
starting a second worker (`serve` beside `runrail worker`) failed the first
one's live runs. Each claim now records its worker, and each worker beats a
heartbeat row; only runs whose worker has stopped beating are recovered.
"""

import sqlalchemy as sa
from alembic import op

revision = "w4f8b2e6d1a7"
down_revision = "v3e7a1d5c9f4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflow_runs") as batch:
        batch.add_column(sa.Column("claimed_by", sa.String(length=64), nullable=True))
    op.create_table(
        "worker_heartbeats",
        sa.Column("owner", sa.String(length=64), primary_key=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("worker_heartbeats")
    with op.batch_alter_table("workflow_runs") as batch:
        batch.drop_column("claimed_by")
