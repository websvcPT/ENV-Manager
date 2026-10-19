"""Add the time of the last git push failure notice

A failing push notifies at its first failure and then at most once a day while the failure lasts,
so the state row has to remember when the last notice of the streak went out.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-19 16:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable time column; a push that is failing now simply notifies again."""
    op.add_column(
        'git_sync_state', sa.Column('failure_notified_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Drop the column in place."""
    op.drop_column('git_sync_state', 'failure_notified_at')
