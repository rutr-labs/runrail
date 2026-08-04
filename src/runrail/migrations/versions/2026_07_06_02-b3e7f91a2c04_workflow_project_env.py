"""workflow_project_environment"""
import sqlalchemy as sa
from alembic import op

revision = 'b3e7f91a2c04'
down_revision = 'cc9cf8e3d4ad'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('workflows', sa.Column('project_id', sa.Integer(), nullable=True))
    op.add_column('workflows', sa.Column('default_environment_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('workflows', 'default_environment_id')
    op.drop_column('workflows', 'project_id')
