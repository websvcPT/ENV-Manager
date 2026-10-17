"""Add environment file attributes

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17 18:02:11.412905
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the schema change."""
    with op.batch_alter_table('environments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('file_mode', sa.String(length=4), nullable=True))
        batch_op.add_column(sa.Column('file_owner', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('file_group', sa.String(length=64), nullable=True))



def downgrade() -> None:
    """Revert the schema change."""
    with op.batch_alter_table('environments', schema=None) as batch_op:
        batch_op.drop_column('file_group')
        batch_op.drop_column('file_owner')
        batch_op.drop_column('file_mode')
