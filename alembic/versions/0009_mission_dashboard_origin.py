'''Argus-Chat als Missions-Herkunft

Revision ID: 0009
Revises: 0008
'''
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    # Gegenstueck zu owui_chat_id fuer den Argus-Chat.
    op.add_column('missions', sa.Column('dashboard_session_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('missions', 'dashboard_session_id')
