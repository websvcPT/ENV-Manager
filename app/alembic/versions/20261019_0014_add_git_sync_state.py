"""Add git sync state

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19 03:07:45.698497
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table that tracks git sync: the watermark, attempt results and the host key."""
    op.create_table('git_sync_state',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('watermark', sa.String(length=64), nullable=True),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=200), nullable=True),
    sa.Column('failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('retry_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('host_key_host', sa.String(length=300), nullable=True),
    sa.Column('host_key', sa.Text(), nullable=True),
    sa.Column('host_key_fingerprint', sa.String(length=100), nullable=True),
    sa.Column('host_key_pinned_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_git_sync_state'))
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table('git_sync_state')
