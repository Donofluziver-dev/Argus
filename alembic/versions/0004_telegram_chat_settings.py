from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'telegram_chat_settings',
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('voice_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('chat_id'),
    )


def downgrade():
    op.drop_table('telegram_chat_settings')
