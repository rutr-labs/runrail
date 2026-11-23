"""workflows that run after other workflows

workflow_upstreams links a workflow to the ones it runs after, instead of a
schedule; workflow_runs.triggered_by_run_id records which upstream run's
success started a chained run; and the "upstream" trigger type names it.
"""

import sqlalchemy as sa
from alembic import op

revision = "a1d7e3c9b5f2"
down_revision = "z8c2e5b9a4d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_upstreams",
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("upstream_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["upstream_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workflow_id", "upstream_id"),
    )
    op.create_index(op.f("ix_workflow_upstreams_upstream_id"), "workflow_upstreams", ["upstream_id"])
    with op.batch_alter_table("workflow_runs") as batch:
        batch.add_column(sa.Column("triggered_by_run_id", sa.Integer(), nullable=True))

    # A new enum label is DDL only on PostgreSQL (see j1a5f37b60c2); on SQLite
    # the column is a plain VARCHAR, and "upstream" fits the length "backfill" set.
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE triggertype ADD VALUE IF NOT EXISTS 'upstream'")


def downgrade():
    with op.batch_alter_table("workflow_runs") as batch:
        batch.drop_column("triggered_by_run_id")
    op.drop_index(op.f("ix_workflow_upstreams_upstream_id"), table_name="workflow_upstreams")
    op.drop_table("workflow_upstreams")
