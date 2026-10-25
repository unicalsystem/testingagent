"""mysql report sync state

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 06:24:37.063055
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive only: safe on a populated database.
    op.add_column('runs', sa.Column('report_synced_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('runs', sa.Column('report_sync_error', sa.Text(), nullable=False, server_default=''))
    # ### end Alembic commands ###


def downgrade() -> None:
    # Additive only: safe on a populated database.
    op.drop_column('runs', 'report_sync_error')
    op.drop_column('runs', 'report_synced_at')
    # ### end Alembic commands ###
