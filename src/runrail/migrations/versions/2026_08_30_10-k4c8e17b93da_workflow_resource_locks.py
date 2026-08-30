"""workflow resource locks"""

import sqlalchemy as sa
from alembic import op

revision = "k4c8e17b93da"
down_revision = "j1a5f37b60c2"
branch_labels = None
depends_on = None

# On PostgreSQL sa.Enum compiles to a native type that op.add_column does NOT
# create for us; on SQLite create()/drop() are no-ops and the column is a bare
# VARCHAR. Existing rows get 'shared', which is inert while lock_resource is NULL.
_LOCK_MODE = sa.Enum("shared", "exclusive", name="lockmode")


def upgrade():
    _LOCK_MODE.create(op.get_bind(), checkfirst=True)
    op.add_column("workflows", sa.Column("lock_resource", sa.String(length=255), nullable=True))
    op.add_column("workflows", sa.Column("lock_mode", _LOCK_MODE,
                                         nullable=False, server_default="shared"))


def downgrade():
    op.drop_column("workflows", "lock_mode")
    op.drop_column("workflows", "lock_resource")
    _LOCK_MODE.drop(op.get_bind(), checkfirst=True)
