"""a record of each retention cleanup pass

The Settings page said what the last cleanup did only from memory, so the
line vanished with a restart, and a pass the scheduler ran at 3am left no
trace at all. One row per pass; the newest few are kept.
"""

import sqlalchemy as sa
from alembic import op

revision = "v3e7a1d5c9f4"
down_revision = "u2d6f9c4e8b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cleanup_passes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runs_deleted", sa.Integer(), nullable=False),
        sa.Column("files_deleted", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("cleanup_passes")
