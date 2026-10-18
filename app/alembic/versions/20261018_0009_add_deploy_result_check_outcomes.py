"""Add deploy result check outcomes

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-18 05:04:14.946731
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the schema change."""
    with op.batch_alter_table('deploy_results', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dist_outcome', sa.Enum('blocked', 'warning', 'passed', 'skipped', 'failed', name='distoutcome', native_enum=False, length=10), nullable=True))
        batch_op.add_column(sa.Column('drift_state', sa.Enum('in_sync', 'drift', 'missing', name='driftstate', native_enum=False, length=10), nullable=True))



def downgrade() -> None:
    """Revert the schema change."""
    with op.batch_alter_table('deploy_results', schema=None) as batch_op:
        batch_op.drop_column('drift_state')
        batch_op.drop_column('dist_outcome')

