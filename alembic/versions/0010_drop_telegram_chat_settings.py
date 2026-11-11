'''Telegram-Anbindung entfernt: Tabelle telegram_chat_settings faellt weg

Revision ID: 0010
Revises: 0009
'''
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    # Hielt Sprachausgabe und Egress-Markierung je Telegram-Chat. Ohne Telegram
    # hat die Tabelle keinen Leser mehr.
    op.execute("DROP TABLE IF EXISTS telegram_chat_settings")


def downgrade():
    op.create_table(
        'telegram_chat_settings',
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('voice_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('has_sensitive_data', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('chat_id'),
    )
