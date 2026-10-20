"""Helpers for the tests of the file type: change the type of a stored environment."""

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Environment, FileType


async def set_file_type(db: AsyncSession, environment_id: int, file_type: FileType) -> None:
    """Store the type of an environment without any of the checks of the settings form."""
    stored = await db.get(Environment, environment_id)
    assert stored is not None
    stored.file_type = file_type
    await db.commit()


async def set_web_file_type(web_app: FastAPI, environment_id: int, file_type: FileType) -> None:
    """Store the type of an environment of the web application's database."""
    async with web_app.state.session_factory() as db:
        await set_file_type(db, environment_id, file_type)
