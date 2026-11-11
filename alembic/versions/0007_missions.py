from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'missions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('goal_encrypted', sa.LargeBinary(), nullable=False),
        sa.Column('context_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('pii_map_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('provider', sa.String(), nullable=False, server_default='gemini'),
        sa.Column('status', sa.String(), nullable=False, server_default='queued'),
        sa.Column('current_step', sa.String(), nullable=True),
        sa.Column('calls_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('result_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_missions_status', 'missions', ['status'])
    op.create_index('ix_missions_created_at', 'missions', ['created_at'])


def downgrade():
    op.drop_index('ix_missions_created_at', table_name='missions')
    op.drop_index('ix_missions_status', table_name='missions')
    op.drop_table('missions')
