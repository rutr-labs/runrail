"""provision managed notebook runtime dependencies"""

import sqlalchemy as sa
from alembic import op

revision = "f8b2c41e9d06"
down_revision = "e2f96a3d710b"
branch_labels = None
depends_on = None


def upgrade():
    environments = sa.table(
        "environments",
        sa.column("managed", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("last_error", sa.Text()),
    )
    op.execute(
        environments.update()
        .where(environments.c.managed.is_(True))
        .values(status="creating", last_error=None)
    )


def downgrade():
    pass
