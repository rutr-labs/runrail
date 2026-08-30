"""drop note author and gate approver"""

import sqlalchemy as sa
from alembic import op

revision = "l2e6b90a4c73"
down_revision = "k4c8e17b93da"
branch_labels = None
depends_on = None


def upgrade():
    # RunRail is single-user and has no accounts, so a captured name was never an
    # identity. The content — why a note was written, why a gate was decided — stays.
    op.drop_column("run_notes", "author")
    op.drop_column("task_runs", "approved_by")


def downgrade():
    op.add_column("task_runs", sa.Column("approved_by", sa.String(length=120), nullable=True))
    op.add_column("run_notes", sa.Column("author", sa.String(length=80), nullable=True))
