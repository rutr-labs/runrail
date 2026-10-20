"""what a task ran with: its code and its environment

task_runs.inputs_json records each attempt's environment (Python, packages)
and its code; task_runs.code_manifest points at the state of the code it
saw. code_manifests and code_blobs hold that code content-addressed: each
distinct file version and each distinct project state stored once. Together
they feed the comparison with the last good run.
"""

import sqlalchemy as sa
from alembic import op

revision = "z8c2e5b9a4d1"
down_revision = "y6b1d4a8f3c9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(sa.Column("inputs_json", sa.JSON, nullable=True))
        batch.add_column(sa.Column("code_manifest", sa.String(length=64), nullable=True))
        batch.create_index("ix_task_runs_code_manifest", ["code_manifest"])
    op.create_table(
        "code_blobs",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("content", sa.LargeBinary, nullable=False),
    )
    op.create_table(
        "code_manifests",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("files", sa.JSON, nullable=False),
    )


def downgrade():
    op.drop_table("code_manifests")
    op.drop_table("code_blobs")
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_index("ix_task_runs_code_manifest")
        batch.drop_column("code_manifest")
        batch.drop_column("inputs_json")
