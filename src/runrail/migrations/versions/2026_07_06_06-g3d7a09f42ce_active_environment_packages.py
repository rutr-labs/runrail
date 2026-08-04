"""track active managed environment packages"""

import sqlalchemy as sa
from alembic import op

revision = "g3d7a09f42ce"
down_revision = "f8b2c41e9d06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "environments",
        sa.Column("active_packages_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.execute(sa.text("UPDATE environments SET active_packages_json = packages_json"))


def downgrade():
    op.drop_column("environments", "active_packages_json")
