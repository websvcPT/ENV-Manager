"""Create application settings

Revision ID: 0001
Revises: 
Create Date: 2026-10-17 14:09:40.764778
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the schema change."""
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_settings'))
    )


def downgrade() -> None:
    """Revert the schema change."""
    op.drop_table('app_settings')
