"""never reuse run, task-run or artifact ids on SQLite

Without AUTOINCREMENT, SQLite hands out max(rowid) + 1, so once retention
deleted the newest runs — schedules paused for longer than the retention
period, say — their ids came back. A run id is a permalink: report links in
Slack and email, /runs/<id>, the per-run log and artifact directories. A reused
one opened a different workflow's run, and a new run adopted the directory of
the old one, stale files and all. PostgreSQL's sequences never go backwards, so
this is SQLite only.

SQLite cannot add AUTOINCREMENT to an existing table, so batch mode rebuilds
each one. The migration connection runs without PRAGMA foreign_keys, which is
what makes dropping the old tables safe: with it on, the drop would cascade
into every child row.
"""

from alembic import op

revision = "o7c3e5a9d2b1"
down_revision = "n5b2d81f3a94"
branch_labels = None
depends_on = None

_TABLES = ("workflow_runs", "task_runs", "artifacts")


def upgrade():
    if op.get_bind().dialect.name != "sqlite":
        return
    for table in _TABLES:
        with op.batch_alter_table(table, recreate="always",
                                  table_kwargs={"sqlite_autoincrement": True}):
            pass


def downgrade():
    if op.get_bind().dialect.name != "sqlite":
        return
    for table in reversed(_TABLES):
        with op.batch_alter_table(table, recreate="always",
                                  table_kwargs={"sqlite_autoincrement": False}):
            pass
