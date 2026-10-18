"""Shared fixtures: template and per-test databases, crypto, seed data, web app, deploy harness.

Every database a test uses is a private copy of a template file that a test worker migrated
(and seeded) once, so the migrations and the seed do not run for each test. The templates are
sealed with one master key for the whole session, which ``crypto`` and ``master_key`` expose.
Tests of the migrations and of the first start build their own database instead.
"""

import copy
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
from app.main import create_app
from app.seeds import DEV_PASSWORD, SeedData
from app.services.deploy import DeployEngine
from app.services.docker_cli import DockerCli
from app.tests.deploy_helpers import Harness, RecordingNotifier, RecordingRunner, set_deployment
from app.tests.dist_helpers import relocate_deployments
from app.tests.template_databases import (
    SeededTemplate,
    build_migrated_template,
    build_seeded_template,
    build_web_template,
    copy_database,
)
from app.tests.web_helpers import EnvironmentIds, environment_ids


@pytest.fixture(autouse=True)
def no_inherited_demo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo project is opt in: ignore a demo root the app container inherited."""
    monkeypatch.delenv("ENVMANAGER_DEMO_ROOT", raising=False)


@pytest.fixture(scope="session")
def master_key() -> str:
    """The master key of the test session: seeded ciphertexts and hashes depend on it."""
    return generate_master_key()


@pytest.fixture(scope="session")
def crypto(master_key: str) -> CryptoService:
    """The crypto service of the session key, the one the template databases were sealed with."""
    return CryptoService(parse_master_key(master_key))


@pytest.fixture(scope="session")
def template_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Folder of this worker's template databases."""
    return tmp_path_factory.mktemp("templates")


@pytest.fixture(scope="session")
def migrated_template(template_dir: Path) -> Path:
    """An empty database migrated to the newest revision."""
    path = template_dir / "migrated.db"
    build_migrated_template(path)
    return path


@pytest.fixture(scope="session")
def seeded_template(
    migrated_template: Path, template_dir: Path, crypto: CryptoService
) -> SeededTemplate:
    """The migrated database holding the seed dataset, with the references the seed returned."""
    return build_seeded_template(migrated_template, template_dir / "seeded.db", crypto)


@pytest.fixture(scope="session")
def web_template(
    seeded_template: SeededTemplate, template_dir: Path, crypto: CryptoService
) -> Path:
    """The seeded database as the application lifespan leaves it: key check record included."""
    path = template_dir / "web.db"
    build_web_template(seeded_template.path, path, crypto)
    return path


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """Path of the test's database file, which does not exist until a fixture copies one there."""
    return tmp_path / "envmanager.db"


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    """Folder the migration backups are written to."""
    return tmp_path / "backups"


@pytest.fixture
async def engine(database_path: Path, migrated_template: Path) -> AsyncIterator[AsyncEngine]:
    """An engine on a private copy of the migrated database, which holds no rows."""
    copy_database(migrated_template, database_path)
    database_engine = create_engine(database_path)
    yield database_engine
    await database_engine.dispose()


@pytest.fixture
async def seeded_database(
    database_path: Path, seeded_template: SeededTemplate
) -> AsyncIterator[AsyncEngine]:
    """An engine on a private copy of the migrated database holding the seed dataset."""
    copy_database(seeded_template.path, database_path)
    database_engine = create_engine(database_path)
    yield database_engine
    await database_engine.dispose()


@pytest.fixture
def seed_data(seeded_database: AsyncEngine, seeded_template: SeededTemplate) -> SeedData:
    """Named references to the seed dataset in ``seeded_database``.

    Ids are the same in every copy of the template. The objects are copied for each test, so a
    test that changes one never changes what another test sees.
    """
    return copy.deepcopy(seeded_template.data)


@pytest.fixture
async def web_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, web_template: Path, master_key: str
) -> AsyncIterator[FastAPI]:
    """The real application with its lifespan running on a temporary data folder and the seed.

    The data folder starts as a copy of the seeded template, so the lifespan finds the database
    at head and the key check record in place, and takes no pre-migration backup.
    """
    copy_database(web_template, tmp_path / "envmanager.db")
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("ENVMANAGER_KEY", master_key)
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    get_settings.cache_clear()
    application = create_app()
    async with application.router.lifespan_context(application):
        application.state.docker_cli = DockerCli(socket_path=tmp_path / "no-docker.sock")
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


@pytest.fixture
async def ids(web_app: FastAPI) -> EnvironmentIds:
    """The seeded environment ids of the web application."""
    return await environment_ids(web_app)


@pytest.fixture
async def harness(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService, tmp_path: Path
) -> AsyncIterator[Harness]:
    """A deploy engine with recording seams on the seeded deployments moved to a real folder.

    The folder is the only allowed path; the seeded owner of the production deployment is
    cleared so deploys there need no privilege.
    """
    async with create_session_factory(seeded_database)() as db:
        roots = await relocate_deployments(db, tmp_path / "host")
        await set_deployment(
            db, seed_data.deployments.rentals_prod.id, file_owner=None, file_group=None
        )
        runner = RecordingRunner()
        notifier = RecordingNotifier()
        lock_dir = tmp_path / "locks"
        engine = DeployEngine(crypto, lock_dir, hook_runner=runner, notifier=notifier)
        yield Harness(
            engine, runner, notifier, lock_dir, roots, seed_data, db, seeded_database, crypto
        )
