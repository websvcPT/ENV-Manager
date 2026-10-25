"""Add Docker registries

The registries the application holds a login for: the host (unique ignoring case), the username
and the token sealed with the master key.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-25 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0022'
down_revision: str | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = 'uq_docker_registries_host_lower'


def upgrade() -> None:
    """Create the table and the unique index on the lowercased host.

    Ids come from a monotonic sequence, so the id a token is sealed to is never handed out again.
    """
    op.create_table('docker_registries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('host', sa.String(length=300), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=False),
    sa.Column('token_encrypted', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_docker_registries')),
    sqlite_autoincrement=True
    )
    op.create_index(INDEX_NAME, 'docker_registries', [sa.text('lower(host)')], unique=True)


def downgrade() -> None:
    """Drop the table with its index; the stored logins are lost."""
    op.drop_index(INDEX_NAME, table_name='docker_registries')
    op.drop_table('docker_registries')
