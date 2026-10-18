"""settings changed from the page, kept in the database

One row per setting that a person has saved from Settings. An environment
variable still wins over a row here (runrail.runtime_settings), so nothing a
deployment pins can be changed from the browser.
"""

import sqlalchemy as sa
from alembic import op

revision = "u2d6f9c4e8b3"
down_revision = "t1c5e8b3d7a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("app_settings")
