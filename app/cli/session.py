"""The database session every command line command works on."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.services import users as user_service


@asynccontextmanager
async def open_session() -> AsyncIterator[AsyncSession]:
    """A database session on the application database, migrated to the current schema."""
    settings = get_settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_path)
    try:
        await migrate_database(engine, settings.database_path, settings.backup_path)
        async with create_session_factory(engine)() as db:
            yield db
    finally:
        await engine.dispose()


def run_command[T](operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run an operation on a fresh session, turning refused operations into exit code 1."""

    async def _run() -> T:
        async with open_session() as db:
            return await operation(db)

    try:
        return asyncio.run(_run())
    except user_service.UserError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from error
