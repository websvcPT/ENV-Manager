"""Create identity tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17 14:12:39.162178
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the schema change."""
    op.create_table('login_counters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('user', 'ip', name='logincounterkind', native_enum=False, length=10), nullable=False),
    sa.Column('subject', sa.String(length=320), nullable=False),
    sa.Column('failures', sa.Integer(), nullable=False),
    sa.Column('window_started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_login_counters')),
    sa.UniqueConstraint('kind', 'subject', name='uq_login_counters_kind_subject')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('group', sa.Enum('superadmin', 'admin', 'viewer', name='usergroup', native_enum=False, length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('totp_secret', sa.Text(), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('name', name=op.f('uq_users_name'))
    )
    op.create_table('api_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('operations', sa.JSON(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_ip', sa.String(length=45), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_api_tokens_owner_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_api_tokens_token_hash'))
    )
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_tokens_owner_id'), ['owner_id'], unique=False)

    op.create_table('user_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_sessions_token_hash'))
    )
    with op.batch_alter_table('user_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_sessions_user_id'), ['user_id'], unique=False)



def downgrade() -> None:
    """Revert the schema change."""
    with op.batch_alter_table('user_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_sessions_user_id'))

    op.drop_table('user_sessions')
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_tokens_owner_id'))

    op.drop_table('api_tokens')
    op.drop_table('users')
    op.drop_table('login_counters')
