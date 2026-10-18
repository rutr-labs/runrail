"""service ids never reused; a crash-count mark; process and thread counts

The supervisor keeps its processes by service id. Without AUTOINCREMENT a
deleted service's id came straight back, and the new service inherited the old
one's live process. `attempts_from` records when a person last cleared a hold
(Start, Resume, an edit), so the exits before it no longer count toward the
next crash loop. `process_count` and `thread_count` join the resource
sample: how big the tree is says whether a worker pool came up.
"""

import sqlalchemy as sa
from alembic import op

revision = "t1c5e8b3d7a2"
down_revision = "s8b1d4f7a2c9"
branch_labels = None
depends_on = None


def upgrade():
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table("services", recreate="always" if sqlite else "auto",
                              table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.add_column(sa.Column("attempts_from", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("process_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("thread_count", sa.Integer(), nullable=True))


def downgrade():
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table("services", recreate="always" if sqlite else "auto",
                              table_kwargs={"sqlite_autoincrement": False}) as batch:
        batch.drop_column("thread_count")
        batch.drop_column("process_count")
        batch.drop_column("attempts_from")
