"""Make user email addresses unique ignoring case

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-18 13:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = 'uq_users_email_lower'


def upgrade() -> None:
    """Refuse to continue while users share an address, then add the unique index.

    The names of the users that share an address are listed (not the addresses), so the
    operator knows which accounts to change before the upgrade can run again.
    """
    shared = op.get_bind().execute(
        sa.text(
            'SELECT group_concat(name, \', \') FROM users '
            'GROUP BY lower(email) HAVING count(*) > 1 ORDER BY min(id)'
        )
    ).scalars().all()
    if shared:
        raise RuntimeError(
            'email addresses must be unique ignoring case, but these users share one: '
            + '; '.join(shared)
            + '. Give each user their own address, then start the application again.'
        )
    op.create_index(INDEX_NAME, 'users', [sa.text('lower(email)')], unique=True)


def downgrade() -> None:
    """Revert the schema change."""
    op.drop_index(INDEX_NAME, table_name='users')
