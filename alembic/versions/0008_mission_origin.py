from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    # Herkunft der Mission fuer "Ergebnis zurueck zur Quelle".
    op.add_column('missions', sa.Column('origin', sa.String(), nullable=False, server_default='telegram'))
    op.add_column('missions', sa.Column('owui_chat_id', sa.String(), nullable=True))
    op.add_column('missions', sa.Column('owui_message_id', sa.String(), nullable=True))
    op.add_column('missions', sa.Column('owui_user_id', sa.String(), nullable=True))


def downgrade():
    op.drop_column('missions', 'owui_user_id')
    op.drop_column('missions', 'owui_message_id')
    op.drop_column('missions', 'owui_chat_id')
    op.drop_column('missions', 'origin')
