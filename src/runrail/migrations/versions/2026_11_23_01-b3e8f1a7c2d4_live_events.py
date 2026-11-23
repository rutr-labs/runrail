"""live events: worker and scheduler updates relayed to the API's sockets

A process with no browsers attached (a `runrail worker` or `runrail scheduler`
started on its own) writes its live-update events to live_events; the API
process tails the table and broadcasts them. See runrail.api.ws.
"""

import sqlalchemy as sa
from alembic import op

revision = "b3e8f1a7c2d4"
down_revision = "a1d7e3c9b5f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "live_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f("ix_live_events_created_at"), "live_events", ["created_at"])


def downgrade():
    op.drop_index(op.f("ix_live_events_created_at"), table_name="live_events")
    op.drop_table("live_events")
