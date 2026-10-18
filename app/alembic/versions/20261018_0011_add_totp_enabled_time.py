"""Add the time two-factor authentication was turned on

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-18 19:13:47.001177
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable time column; accounts that already have a second factor keep it empty."""
    op.add_column('users', sa.Column('totp_enabled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the column in place: a table rebuild would lose the expression index on the email."""
    op.drop_column('users', 'totp_enabled_at')
