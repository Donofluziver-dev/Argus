from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'reflection_state',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('reflected_until', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('user_id'),
    )


def downgrade():
    op.drop_table('reflection_state')
