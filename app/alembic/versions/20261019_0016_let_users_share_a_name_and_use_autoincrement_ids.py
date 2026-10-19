"""Let users share a name and use autoincrement ids

The email address is the key of a user and the name is only a display name, so the unique
constraint on the name goes. The table is rebuilt with a monotonic id sequence in the same step,
so the id of a removed user is never handed out again to a later one.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-19 15:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME_CONSTRAINT = 'uq_users_name'
EMAIL_INDEX = 'uq_users_email_lower'


def _rebuild_users(*, shared_names: bool) -> None:
    """Rebuild the users table, keeping every row, and put the email index back.

    Dropping the table with foreign keys enforced would run its cascades and delete the sessions,
    grants and tokens of every user, which is why the migration runner of the application
    (``upgrade_to_head``) switches enforcement off before its transaction starts. The email index
    is on an expression, which the table rebuild does not carry over, so it is dropped before and
    created again after.
    """
    op.execute(f'DROP INDEX IF EXISTS {EMAIL_INDEX}')
    with op.batch_alter_table(
        'users', recreate='always', table_kwargs={'sqlite_autoincrement': shared_names}
    ) as batch_op:
        if shared_names:
            batch_op.drop_constraint(NAME_CONSTRAINT, type_='unique')
        else:
            batch_op.create_unique_constraint(NAME_CONSTRAINT, ['name'])
    op.create_index(EMAIL_INDEX, 'users', [sa.text('lower(email)')], unique=True)


def upgrade() -> None:
    """Apply the schema change; names and ids are kept."""
    _rebuild_users(shared_names=True)


def downgrade() -> None:
    """Revert the schema change; refused while two users share a name."""
    shared = op.get_bind().execute(
        sa.text(
            'SELECT group_concat(name, \', \') FROM users '
            'GROUP BY name HAVING count(*) > 1 ORDER BY min(id)'
        )
    ).scalars().all()
    if shared:
        raise RuntimeError(
            'user names must be unique to go back to the earlier schema, but these names are '
            'shared: ' + '; '.join(shared)
        )
    _rebuild_users(shared_names=False)
