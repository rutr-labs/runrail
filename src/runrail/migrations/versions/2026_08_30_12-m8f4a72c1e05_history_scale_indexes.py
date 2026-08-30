"""indexes for a database with real history

Measured on a seeded home of 59k runs / 194k task runs (scripts/seed_demo.py):

  GET /api/runs?workflow_id=N          10.4 ms -> 0.1 ms
  GET /api/stats/summary (per count)    7.7 ms -> 0.0 ms
  activity feed's SLA scan              3.8 ms -> 0.0 ms
  DELETE one task (FK cascade)         54.4 ms -> 7.4 ms
  retention: delete 500 old runs       97.4 ms -> 6.1 ms

The two workflow_runs indexes REPLACE their single-column ancestors rather than
joining them: a composite whose leading column is the old one answers everything
the old one answered, so keeping both would only cost writes.
"""

from alembic import op

revision = "m8f4a72c1e05"
down_revision = "l2e6b90a4c73"
branch_labels = None
depends_on = None


def upgrade():
    # Filtered run lists are always ordered newest-first; on the single-column
    # index the database read a workflow's whole history and sorted it to
    # return one page.
    op.drop_index("ix_workflow_runs_workflow_id", table_name="workflow_runs")
    op.create_index("ix_workflow_runs_workflow_id_created_at", "workflow_runs",
                    ["workflow_id", "created_at"])
    op.drop_index("ix_workflow_runs_status", table_name="workflow_runs")
    op.create_index("ix_workflow_runs_status_created_at", "workflow_runs",
                    ["status", "created_at"])
    # /api/stats/summary counts "created in the last day AND status = x" three
    # times per poll, and the activity feed's transition scan is bounded the
    # same way.
    op.create_index("ix_workflow_runs_sla_breached_at", "workflow_runs", ["sla_breached_at"])
    # Both are foreign keys with a CASCADE and no index, so every delete of a
    # parent row scanned the entire child table. Retention and editing a
    # workflow's tasks are the two places that hurt.
    op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])
    op.create_index("ix_artifacts_task_run_id", "artifacts", ["task_run_id"])


def downgrade():
    op.drop_index("ix_artifacts_task_run_id", table_name="artifacts")
    op.drop_index("ix_task_runs_task_id", table_name="task_runs")
    op.drop_index("ix_workflow_runs_sla_breached_at", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_status_created_at", table_name="workflow_runs")
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.drop_index("ix_workflow_runs_workflow_id_created_at", table_name="workflow_runs")
    op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
