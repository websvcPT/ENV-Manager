"""Add notification records

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-18 23:59:45.186784
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the seen address and delivery status tables."""
    op.create_table('notification_status',
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('failures', sa.Integer(), nullable=False),
    sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.PrimaryKeyConstraint('channel', name=op.f('pk_notification_status'))
    )
    op.create_table('user_seen_addresses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('address_hash', sa.String(length=64), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_seen_addresses_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_seen_addresses')),
    sa.UniqueConstraint('user_id', 'address_hash', name='uq_seen_address_user')
    )
    with op.batch_alter_table('user_seen_addresses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_seen_addresses_user_id'), ['user_id'], unique=False)



def downgrade() -> None:
    """Drop both tables."""
    with op.batch_alter_table('user_seen_addresses', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_seen_addresses_user_id'))

    op.drop_table('user_seen_addresses')
    op.drop_table('notification_status')
