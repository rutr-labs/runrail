"""per-workflow schedule timezone"""

import sqlalchemy as sa
from alembic import op

revision = "i9c4d82e5b17"
down_revision = "h5e2b71c9a3d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workflows", sa.Column("schedule_timezone", sa.String(length=64), nullable=True))


def downgrade():
    op.drop_column("workflows", "schedule_timezone")
