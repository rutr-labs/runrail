"""managed environment lifecycle"""

import sqlalchemy as sa
from alembic import op

revision = "d7a4b18c2f10"
down_revision = "b3e7f91a2c04"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("environments", sa.Column("managed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("environments", sa.Column("status", sa.String(length=20), nullable=False, server_default="ready"))
    op.add_column("environments", sa.Column("base_executable", sa.Text(), nullable=True))
    op.add_column("environments", sa.Column("packages_json", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("environments", sa.Column("python_version", sa.String(length=100), nullable=True))
    op.add_column("environments", sa.Column("build_log", sa.Text(), nullable=True))
    op.add_column("environments", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("environments", sa.Column("last_built_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("environments", sa.Column("build_started_at", sa.DateTime(timezone=True), nullable=True))
    environments = sa.table(
        "environments",
        sa.column("env_type", sa.String()),
        sa.column("executable", sa.Text()),
        sa.column("conda_env", sa.String()),
        sa.column("status", sa.String()),
        sa.column("last_error", sa.Text()),
    )
    op.execute(
        environments.update()
        .where(
            sa.or_(
                sa.and_(environments.c.env_type == "conda", environments.c.conda_env.is_(None)),
                sa.and_(environments.c.env_type != "conda", environments.c.executable.is_(None)),
            )
        )
        .values(status="failed", last_error="Legacy environment configuration is incomplete; edit and validate it")
    )


def downgrade():
    for column in (
        "build_started_at", "last_built_at", "last_error", "build_log", "python_version",
        "packages_json", "base_executable", "status", "managed",
    ):
        op.drop_column("environments", column)
