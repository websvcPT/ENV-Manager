"""Make deployment names unique in their project ignoring case

Deployment names become display names (letters in any case, spaces), so two deployments of a
project may not differ only in case.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-21 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0021'
down_revision: str | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = 'uq_deployments_project_name_lower'


def upgrade() -> None:
    """Refuse to continue while deployments of a project share a name ignoring case, then add the
    unique index.

    Deployment names were lowercase slugs until now, so no database holds such a pair unless it
    was edited by hand. The message names the deployments, so the operator knows which to rename.
    """
    shared = op.get_bind().execute(
        sa.text(
            'SELECT group_concat(name, \', \') FROM '
            '(SELECT id, project_id, name FROM deployments ORDER BY id) '
            'GROUP BY project_id, lower(name) HAVING count(*) > 1 ORDER BY min(id)'
        )
    ).scalars().all()
    if shared:
        raise RuntimeError(
            'deployment names must be unique in their project ignoring case, but these '
            'deployments share a name: '
            + '; '.join(shared)
            + '. Give each deployment its own name, then start the application again.'
        )
    op.create_index(
        INDEX_NAME, 'deployments', ['project_id', sa.text('lower(name)')], unique=True
    )


def downgrade() -> None:
    """Drop the index; names with capitals or spaces stay as they are."""
    op.drop_index(INDEX_NAME, table_name='deployments')
