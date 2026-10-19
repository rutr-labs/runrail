"""a workflow's run form: the parameters a run takes

A list of typed parameters with defaults, read by "Run with…" and checked
whenever a run starts. NULL, every existing workflow, is no form.
"""

import sqlalchemy as sa
from alembic import op

revision = "y6b1d4a8f3c9"
down_revision = "x5a9c3f7e2b8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflows") as batch:
        batch.add_column(sa.Column("run_parameters_json", sa.JSON, nullable=True))


def downgrade():
    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("run_parameters_json")
