"""workflow failure notifications and auto-pause"""

import sqlalchemy as sa
from alembic import op

revision = "h5e2b71c9a3d"
down_revision = "g3d7a09f42ce"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workflows", sa.Column("notify_webhook_url", sa.Text(), nullable=True))
    op.add_column("workflows", sa.Column("auto_pause_failures", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("workflows", "auto_pause_failures")
    op.drop_column("workflows", "notify_webhook_url")
