"""Add single sign-on

``user_identities`` holds the provider identity (issuer and subject) a user signs in with: one
per user and one user per identity. ``sso_logins`` holds the sign-ins that are between the
redirect to the provider and its callback: hashes of the state and of the browser binding token,
the nonce, and the PKCE code verifier sealed with the master key. Users created at their first
sign in keep an unusable password hash, so the users table does not change.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0024'
down_revision: str | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create both tables; no existing row needs anything."""
    op.create_table('user_identities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('issuer', sa.String(length=2048), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('linked_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_identities_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_identities')),
    sa.UniqueConstraint('issuer', 'subject', name='uq_user_identities_issuer_subject'),
    sa.UniqueConstraint('user_id', name=op.f('uq_user_identities_user_id'))
    )
    op.create_table('sso_logins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('state_hash', sa.String(length=64), nullable=False),
    sa.Column('binding_hash', sa.String(length=64), nullable=False),
    sa.Column('nonce', sa.String(length=128), nullable=False),
    sa.Column('code_verifier_encrypted', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sso_logins')),
    sa.UniqueConstraint('state_hash', name=op.f('uq_sso_logins_state_hash'))
    )


def downgrade() -> None:
    """Drop both tables; the links to provider identities and the sign-ins in progress are lost,
    and a user created at a first sign in keeps the account with its unusable password."""
    op.drop_table('sso_logins')
    op.drop_table('user_identities')
