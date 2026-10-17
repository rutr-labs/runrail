"""alert state recorded where the decision is made

The failure/recovery transition, the auto-pause, and which alert a run sent
were reconstructed afterwards from run ids. That lost the recovery when a
failed run was resumed and succeeded, sent a recovery for a workflow still
failing, double-sent when two runs finished together, and re-paused a workflow
one failure after it was switched back on. These columns let notify.py record
each decision under a compare-and-set, and the feed read what was decided.

`resumed_at` anchors the SLA deadline after a resume; `webhook_error` keeps
the last delivery failure visible.
"""

import sqlalchemy as sa
from alembic import op

revision = "r3f7a9c2e6b4"
down_revision = "q9e2c4a7b5d3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflows", schema=None) as batch:
        batch.add_column(sa.Column("alert_state", sa.String(16)))
        batch.add_column(sa.Column("alert_run_id", sa.Integer))
        batch.add_column(sa.Column("enabled_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("auto_paused_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("auto_paused_run_id", sa.Integer))
        batch.add_column(sa.Column("webhook_error", sa.Text))
        batch.add_column(sa.Column("webhook_error_at", sa.DateTime(timezone=True)))
    with op.batch_alter_table("workflow_runs", schema=None) as batch:
        batch.add_column(sa.Column("resumed_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("alert_kind", sa.String(32)))
    # Existing workflows start from their newest completed outcome, so the
    # first run after upgrading alerts only if it changes something.
    op.execute(sa.text("""
        UPDATE workflows SET alert_state = (
            SELECT CASE WHEN r.status = 'failed' THEN 'failing' ELSE 'ok' END
            FROM workflow_runs r
            WHERE r.workflow_id = workflows.id AND r.status IN ('success', 'failed')
            ORDER BY r.id DESC LIMIT 1)
    """))


def downgrade():
    with op.batch_alter_table("workflow_runs", schema=None) as batch:
        batch.drop_column("alert_kind")
        batch.drop_column("resumed_at")
    with op.batch_alter_table("workflows", schema=None) as batch:
        for column in ("webhook_error_at", "webhook_error", "auto_paused_run_id",
                       "auto_paused_at", "enabled_at", "alert_run_id", "alert_state"):
            batch.drop_column(column)
