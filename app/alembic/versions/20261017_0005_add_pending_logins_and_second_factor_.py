"""Add pending logins and second factor replay marker

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17 16:02:30.599760
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the schema change."""
    op.create_table('pending_logins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_pending_logins_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pending_logins')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_pending_logins_token_hash'))
    )
    with op.batch_alter_table('pending_logins', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pending_logins_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_last_step', sa.Integer(), nullable=True))



def downgrade() -> None:
    """Revert the schema change."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('totp_last_step')

    with op.batch_alter_table('pending_logins', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pending_logins_user_id'))

    op.drop_table('pending_logins')
