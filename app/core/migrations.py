"""Run Alembic migrations from the application, after an automatic backup."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.log import get_logger
from app.services.backup import create_backup

log = get_logger("migrations")

ALEMBIC_DIR = Path(__file__).resolve().parent.parent / "alembic"


def build_config(connection: Connection | None = None) -> Config:
    """Build an Alembic config pointing at the packaged migration scripts."""
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _current_and_head(connection: Connection) -> tuple[str | None, str | None]:
    """Return the database revision and the newest script revision."""
    current = MigrationContext.configure(connection).get_current_revision()
    head = ScriptDirectory.from_config(build_config()).get_current_head()
    return current, head


def _upgrade(connection: Connection) -> None:
    """Upgrade the database behind ``connection`` to the newest revision."""
    command.upgrade(build_config(connection), "head")


async def upgrade_to_head(engine: AsyncEngine) -> None:
    """Upgrade on one connection with foreign key enforcement off, then check and restore it.

    Rebuilding a table that other tables reference would cascade deletes into them while
    enforcement is on, and the setting cannot change inside a transaction, so it is switched
    before the first statement. Leftover violations fail the upgrade and roll it back.
    """
    async with engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            await connection.run_sync(_upgrade)
            violations = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if violations:
                raise RuntimeError(f"{len(violations)} foreign key violations after migrating")
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")


async def migrate_database(engine: AsyncEngine, database_path: Path, backup_dir: Path) -> None:
    """Bring the schema to head, backing up an existing database before changing it."""
    async with engine.connect() as connection:
        current, head = await connection.run_sync(_current_and_head)
    if current == head:
        return
    if current is not None and database_path.exists():
        backup = await create_backup(database_path, backup_dir, "pre-migration")
        log.info("database backed up to %s before migration", backup.name)
    await upgrade_to_head(engine)
    log.info("database migrated from %s to %s", current, head)
