from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('audit_log', sa.Column('details', sa.String(), nullable=True))


def downgrade():
    op.drop_column('audit_log', 'details')
