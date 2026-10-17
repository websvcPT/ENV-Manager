"""Shared fixtures: temporary SQLite database, crypto service, seed data and a seeded web app."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.csrf import CSRF_COOKIE_NAME
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.main import create_app
from app.seeds import DEV_PASSWORD, SeedData, seed


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """Path of a database file that does not exist yet."""
    return tmp_path / "envmanager.db"


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    """Folder the migration backups are written to."""
    return tmp_path / "backups"


@pytest.fixture
async def engine(database_path: Path, backup_dir: Path) -> AsyncIterator[AsyncEngine]:
    """An engine on a freshly migrated temporary database."""
    database_engine = create_engine(database_path)
    await migrate_database(database_engine, database_path, backup_dir)
    yield database_engine
    await database_engine.dispose()


@pytest.fixture
def crypto() -> CryptoService:
    """A crypto service on a fresh random key."""
    return CryptoService(parse_master_key(generate_master_key()))


@pytest.fixture
async def seed_data(engine: AsyncEngine, crypto: CryptoService) -> SeedData:
    """The seed dataset created in the temporary database, with named references."""
    async with create_session_factory(engine)() as session:
        return await seed(session, crypto)


@pytest.fixture
async def seeded_database(engine: AsyncEngine, seed_data: SeedData) -> AsyncEngine:
    """The temporary database engine after the seed dataset has been created."""
    return engine


@pytest.fixture
async def web_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The real application with its lifespan running on a temporary data folder and the seed."""
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    get_settings.cache_clear()
    application = create_app()
    async with application.router.lifespan_context(application):
        async with application.state.session_factory() as session:
            await seed(session, application.state.crypto)
        yield application
    get_settings.cache_clear()


@pytest.fixture
async def web_client(web_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A browser-like client (asks for HTML) on the seeded application, signed out."""
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Accept": "text/html"}
    ) as http:
        yield http


async def sign_in(http: httpx.AsyncClient, name: str) -> None:
    """Sign a seeded user in through the login route, with the CSRF token it requires."""
    await http.get("/healthz")
    response = await http.post(
        "/login",
        data={
            "identifier": name,
            "password": DEV_PASSWORD,
            "csrf_token": http.cookies[CSRF_COOKIE_NAME],
        },
    )
    assert response.status_code == 303
