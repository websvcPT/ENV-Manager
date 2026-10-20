"""Add the type of an environment file

Every file is either an ENV file (KEY=VALUE text) or a certificate (PEM text). The type is a
string column with a check constraint, so a later type needs one migration and no new column.
Files that exist before the migration are ENV files. The table is rebuilt to add the check
constraint, which SQLite cannot add in place; rows, ids and the autoincrement sequence are kept.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = 'ck_environments_file_type'


def _require_foreign_keys_off() -> None:
    """Refuse to rebuild the table while foreign keys are enforced.

    Dropping the old table with enforcement on would run the cascades of its children (versions,
    grants, restrictions) and the set-null rules of the deploy results, so the migration runner
    must switch enforcement off before its transaction starts (the setting cannot change inside
    one).
    """
    bind = op.get_bind()
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("this migration must run with foreign key enforcement switched off")


def upgrade() -> None:
    """Add the type column, every existing file being an ENV file, and its check constraint."""
    _require_foreign_keys_off()
    with op.batch_alter_table(
        'environments', recreate='always', table_kwargs={'sqlite_autoincrement': True}
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                'file_type',
                sa.Enum('env', 'cert', name='filetype', native_enum=False, length=10),
                server_default='env',
                nullable=False,
            )
        )
        batch_op.create_check_constraint(op.f(CONSTRAINT), "file_type IN ('env', 'cert')")


def downgrade() -> None:
    """Drop the constraint and the column; the type of each file is lost."""
    _require_foreign_keys_off()
    with op.batch_alter_table(
        'environments', recreate='always', table_kwargs={'sqlite_autoincrement': True}
    ) as batch_op:
        batch_op.drop_constraint(op.f(CONSTRAINT), type_='check')
        batch_op.drop_column('file_type')
