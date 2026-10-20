"""what a task ran with: its scripts' versions and its environment

task_runs.inputs_json records each attempt's script hashes and package
versions; source_snapshots keeps each distinct script version once. Both
feed the comparison with the last good run.
"""

import sqlalchemy as sa
from alembic import op

revision = "z8c2e5b9a4d1"
down_revision = "y6b1d4a8f3c9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(sa.Column("inputs_json", sa.JSON, nullable=True))
    op.create_table(
        "source_snapshots",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_snapshots_last_seen", "source_snapshots", ["last_seen"])


def downgrade():
    op.drop_index("ix_source_snapshots_last_seen", table_name="source_snapshots")
    op.drop_table("source_snapshots")
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_column("inputs_json")
