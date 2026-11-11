from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ingested_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(), nullable=True),
        sa.Column('file_path', sa.String(), nullable=True),
        sa.Column('file_hash', sa.String(), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=True),
        sa.Column('ingested_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ingested_documents_filename'), 'ingested_documents', ['filename'], unique=True)


def downgrade():
    op.drop_index(op.f('ix_ingested_documents_filename'), table_name='ingested_documents')
    op.drop_table('ingested_documents')
