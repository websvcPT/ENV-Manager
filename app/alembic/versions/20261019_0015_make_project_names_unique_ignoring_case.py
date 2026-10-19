"""Make project names unique ignoring case

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-19 07:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = 'uq_projects_name_lower'


def upgrade() -> None:
    """Refuse to continue while projects share a name ignoring case, then add the unique index.

    Project names were lowercase only until now, so no database can hold such a pair unless it
    was edited by hand. The message names the projects, so the operator knows which to rename.
    """
    shared = op.get_bind().execute(
        sa.text(
            'SELECT group_concat(name, \', \') FROM projects '
            'GROUP BY lower(name) HAVING count(*) > 1 ORDER BY min(id)'
        )
    ).scalars().all()
    if shared:
        raise RuntimeError(
            'project names must be unique ignoring case, but these projects share a name: '
            + '; '.join(shared)
            + '. Give each project its own name, then start the application again.'
        )
    op.create_index(INDEX_NAME, 'projects', [sa.text('lower(name)')], unique=True)


def downgrade() -> None:
    """Revert the schema change."""
    op.drop_index(INDEX_NAME, table_name='projects')
