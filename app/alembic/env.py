"""Alembic environment: uses the application's models and database location."""

import asyncio

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.core.db import sqlite_url
from app.models import Base
from app.models.types import UtcDateTime

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | bool:
    """Render the custom datetime type as the plain SQLAlchemy type it stores as."""
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations(connection: Connection) -> None:
    """Run migrations on an open connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an engine on the configured database file and run migrations."""
    engine = create_async_engine(sqlite_url(get_settings().database_path))
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


def run_online() -> None:
    """Reuse the caller's connection when given, otherwise open one."""
    connection = context.config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


run_online()
