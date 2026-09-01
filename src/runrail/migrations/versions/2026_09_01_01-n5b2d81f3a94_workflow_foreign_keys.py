"""foreign keys for workflows.project_id and default_environment_id

The model has declared both as ``ForeignKey(..., ondelete="SET NULL")`` since
they were introduced, but the migration that added the columns used a bare
``add_column`` with no constraint — so the rule existed in Python and nowhere in
the database, on SQLite and PostgreSQL alike.

The consequences were live, not theoretical. Deleting an environment left every
workflow that used it as default pointing at a dead id: /run, /backfill and
/resume all answer 404 "Environment not found", so a failed run can never be
resumed, while the scheduler and worker — which never re-validate — keep
enqueueing and executing that workflow's shell tasks. Deleting a project was
quieter and worse: tasks stopped resolving against the project root and started
running in whatever directory RunRail itself was launched from, so every
relative path read and wrote the wrong tree.

Dangling ids are cleared before the constraints go on, because an existing
database can already contain them.
"""

from alembic import op

revision = "n5b2d81f3a94"
down_revision = "m8f4a72c1e05"
branch_labels = None
depends_on = None


def upgrade():
    # Existing rows first: a constraint cannot be added over data that violates
    # it, and these ids are exactly the wreckage the missing constraint allowed.
    op.execute("""
        UPDATE workflows SET project_id = NULL
        WHERE project_id IS NOT NULL
          AND project_id NOT IN (SELECT id FROM projects)
    """)
    op.execute("""
        UPDATE workflows SET default_environment_id = NULL
        WHERE default_environment_id IS NOT NULL
          AND default_environment_id NOT IN (SELECT id FROM environments)
    """)
    # batch_alter_table so SQLite gets this too: it cannot ADD CONSTRAINT, and
    # batch mode rebuilds the table to apply it.
    with op.batch_alter_table("workflows", schema=None) as batch:
        batch.create_foreign_key(
            "fk_workflows_project_id", "projects", ["project_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_workflows_default_environment_id", "environments",
            ["default_environment_id"], ["id"], ondelete="SET NULL",
        )


def downgrade():
    with op.batch_alter_table("workflows", schema=None) as batch:
        batch.drop_constraint("fk_workflows_default_environment_id", type_="foreignkey")
        batch.drop_constraint("fk_workflows_project_id", type_="foreignkey")
