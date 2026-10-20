"""global variables and secrets

One table for both: a variable's value is stored as typed, a secret's
encrypted with the key in <home>/secrets.key (see runrail.variables).
"""

import sqlalchemy as sa
from alembic import op

revision = "x5a9c3f7e2b8"
down_revision = "w4f8b2e6d1a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "variables",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("secret", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("variables")
