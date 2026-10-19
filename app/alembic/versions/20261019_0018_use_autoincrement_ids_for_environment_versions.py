"""Use autoincrement ids for environment versions

Without AUTOINCREMENT SQLite reuses the largest rowid after that row is deleted, so the version
ids kept by audit entries and deploy results of a deleted file could name the versions of a file
saved later. The table is rebuilt with a monotonic id sequence; rows and content are kept.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-19 16:10:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild(autoincrement: bool) -> None:
    """Rebuild the versions table, keeping every row.

    Dropping the table with foreign keys enforced would run the cascades of its parents and the
    set-null rules of the deploy results that point at it, so the migration runner must switch
    enforcement off before its transaction starts (the setting cannot change inside one). Refuse
    to run otherwise.
    """
    bind = op.get_bind()
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("this migration must run with foreign key enforcement switched off")
    with op.batch_alter_table(
        "environment_versions",
        recreate="always",
        table_kwargs={"sqlite_autoincrement": autoincrement},
    ):
        pass


def upgrade() -> None:
    """Apply the schema change."""
    _rebuild(autoincrement=True)


def downgrade() -> None:
    """Revert the schema change."""
    _rebuild(autoincrement=False)
