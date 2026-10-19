"""Add the extra notification addresses of a project

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19 09:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the list column in place; every project starts with no extra address."""
    op.add_column('projects', sa.Column('extra_addresses', sa.JSON(), server_default='[]', nullable=False))


def downgrade() -> None:
    """Drop the column in place: a table rebuild would lose the autoincrement of the ids."""
    op.drop_column('projects', 'extra_addresses')
