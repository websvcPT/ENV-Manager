"""Use autoincrement ids for structure tables

Without AUTOINCREMENT SQLite reuses the largest rowid after that row is deleted, which would
attach audit entries of a deleted item (they keep plain scope ids, no foreign keys) to a new
item. The tables are rebuilt with a monotonic id sequence.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17 16:19:54.029430
"""

from collections.abc import Sequence

from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STRUCTURE_TABLES = ("projects", "deployments", "services", "environments")


def _rebuild(autoincrement: bool) -> None:
    """Rebuild the structure tables, parents first.

    Dropping a parent table with foreign keys enforced would run its cascades and delete the
    children, so the migration runner must switch enforcement off before its transaction starts
    (the setting cannot change inside one). Refuse to run otherwise.
    """
    bind = op.get_bind()
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("this migration must run with foreign key enforcement switched off")
    for table in STRUCTURE_TABLES:
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": autoincrement}
        ):
            pass


def upgrade() -> None:
    """Apply the schema change."""
    _rebuild(autoincrement=True)


def downgrade() -> None:
    """Revert the schema change."""
    _rebuild(autoincrement=False)
