"""what a service's process is using

CPU and resident memory of a service's process tree, sampled by the supervisor
every few seconds. Three nullable columns on services.
"""

import sqlalchemy as sa
from alembic import op

revision = "s8b1d4f7a2c9"
down_revision = "r3f7a9c2e6b4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("services", schema=None) as batch:
        batch.add_column(sa.Column("cpu_percent", sa.Float))
        batch.add_column(sa.Column("rss_bytes", sa.BigInteger))
        batch.add_column(sa.Column("resources_at", sa.DateTime(timezone=True)))


def downgrade():
    with op.batch_alter_table("services", schema=None) as batch:
        batch.drop_column("resources_at")
        batch.drop_column("rss_bytes")
        batch.drop_column("cpu_percent")
