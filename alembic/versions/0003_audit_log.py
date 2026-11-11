from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('tool', sa.String(), nullable=False),
        sa.Column('tool_class', sa.String(), nullable=False),
        sa.Column('input_hash', sa.String(), nullable=False),
        sa.Column('output_hash', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('hmac_signature', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_log_ts'), 'audit_log', ['ts'])
    op.create_index(op.f('ix_audit_log_tool'), 'audit_log', ['tool'])


def downgrade():
    op.drop_index(op.f('ix_audit_log_tool'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_ts'), table_name='audit_log')
    op.drop_table('audit_log')
