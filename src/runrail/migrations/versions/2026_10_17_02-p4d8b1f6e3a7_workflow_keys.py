"""a stable key per workflow, so YAML apply survives a rename

Apply matched workflows by name, so renaming `etl` to `etl-v2` in the UI and
then re-applying the file that still says `etl` — from CI, say — created a
second `etl` with the same schedule and tasks, and both fired. The key is
written into every export and matched before the name; files without one keep
matching by name.
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "p4d8b1f6e3a7"
down_revision = "o7c3e5a9d2b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflows", schema=None) as batch:
        batch.add_column(sa.Column("key", sa.String(32), nullable=True))
    workflows = sa.table("workflows", sa.column("id", sa.Integer), sa.column("key", sa.String))
    bind = op.get_bind()
    for (workflow_id,) in bind.execute(sa.select(workflows.c.id)).all():
        bind.execute(workflows.update().where(workflows.c.id == workflow_id)
                     .values(key=uuid.uuid4().hex))
    with op.batch_alter_table("workflows", schema=None) as batch:
        batch.alter_column("key", existing_type=sa.String(32), nullable=False)
        batch.create_unique_constraint("uq_workflows_key", ["key"])


def downgrade():
    with op.batch_alter_table("workflows", schema=None) as batch:
        batch.drop_constraint("uq_workflows_key", type_="unique")
        batch.drop_column("key")
