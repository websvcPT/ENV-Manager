"""Tests for the seed dataset and the dev seed command."""

import asyncio
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from pathlib import Path

import pyotp
import pytest
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli.main import app
from app.config import get_settings
from app.core.crypto import CryptoService, generate_master_key
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.core.passwords import verify_password
from app.models import (
    AccessGrant,
    AllowedPath,
    ApiToken,
    ApiTokenRestriction,
    AuditActorType,
    AuditEntry,
    AuditResult,
    Base,
    Deployment,
    DeployResult,
    DeployRun,
    Environment,
    EnvironmentVersion,
    HookType,
    LoginCounter,
    LoginCounterKind,
    Project,
    Service,
    ServiceHook,
    User,
)
from app.models.types import utc_now
from app.seeds import (
    BULK_ACTION,
    BULK_COUNT,
    DELETED_PROJECT_ID,
    DEV_ALLOWED_ROOT,
    DEV_PASSWORD,
    DEV_TOTP_SECRET,
    FANCY_PROJECT_NAME,
    FIRST_FIGURE_DAY,
    LAST_FIGURE_DAY,
    STAGING_WEB_COMMAND,
    SeedData,
    SeedError,
    ensure_activity,
    ensure_audit_figures,
    ensure_literal_commands,
    ensure_project_display_name,
    ensure_service_figures,
    seed,
)
from app.services import totp
from app.tests.auth_helpers import make_user
from app.tests.template_databases import SeededTemplate

runner = CliRunner()


async def test_seed_creates_the_dataset(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService
) -> None:
    async with create_session_factory(seeded_database)() as db:
        users = list((await db.execute(select(User))).scalars())
        assert len(users) == 10
        assert all(verify_password(DEV_PASSWORD, user.password_hash)[0] for user in users)
        assert (await db.execute(select(func.count()).select_from(AccessGrant))).scalar_one() == 8
        assert (await db.execute(select(func.count()).select_from(Environment))).scalar_one() == 10
        versions = (
            (
                await db.execute(
                    select(EnvironmentVersion).where(
                        EnvironmentVersion.environment_id == seed_data.envs.db.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [version.number for version in versions] == [1, 2]
        assert crypto.decrypt_text(versions[1].content_encrypted) == "DB_HOST=db\nDB_PORT=3306\n"
        assert versions[1].content_hash == crypto.content_hash("DB_HOST=db\nDB_PORT=3306\n")


def _referenced_rows(value: object) -> Iterator[Base]:
    """The mapped objects a seed result names, however deep in tuples and dataclasses."""
    if isinstance(value, Base):
        yield value
    elif isinstance(value, tuple):
        for item in value:
            yield from _referenced_rows(item)
    elif is_dataclass(value):
        for field in fields(value):
            yield from _referenced_rows(getattr(value, field.name))


async def test_the_references_of_every_copy_of_the_seed_name_its_own_rows(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Ids are deterministic, so the references built with the template match each copy."""
    referenced = list(_referenced_rows(seed_data))
    assert {type(row) for row in referenced} >= {
        User,
        Project,
        Deployment,
        Service,
        Environment,
        AllowedPath,
        DeployRun,
        ApiToken,
    }
    async with create_session_factory(seeded_database)() as db:
        for expected in referenced:
            columns = [attribute.key for attribute in inspect(type(expected)).column_attrs]
            identity = inspect(expected).identity
            assert identity is not None, expected
            stored = await db.get(type(expected), identity)
            assert stored is not None, (type(expected).__name__, identity)
            assert {key: getattr(stored, key) for key in columns} == {
                key: getattr(expected, key) for key in columns
            }, (type(expected).__name__, identity)


def test_a_test_changes_only_its_own_copy_of_the_seed_references(
    seed_data: SeedData, seeded_template: SeededTemplate
) -> None:
    """The references handed to a test are copies, never the objects the template built."""
    shared = seeded_template.data
    assert seed_data is not shared and seed_data.users.viewer is not shared.users.viewer
    seed_data.users.viewer.is_active = False
    assert shared.users.viewer.is_active


async def test_seeding_twice_is_refused(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService
) -> None:
    async with create_session_factory(seeded_database)() as db:
        with pytest.raises(SeedError, match="already seeded"):
            await seed(db, crypto)


@pytest.fixture
def cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at an empty data folder with a generated master key."""
    data_path = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(data_path))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    get_settings.cache_clear()
    yield data_path
    get_settings.cache_clear()


def _user_count(data_path: Path) -> int:
    """Number of users in the CLI's database."""

    async def count() -> int:
        engine = create_engine(data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                return (await db.execute(select(func.count()).select_from(User))).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(count())


@pytest.mark.parametrize("environment", ["testing", "development"])
def test_dev_seed_command_seeds_in_allowed_environments(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", environment)
    get_settings.cache_clear()
    result = runner.invoke(app, ["dev", "seed"])
    assert result.exit_code == 0, result.output
    assert DEV_PASSWORD in result.output
    assert _user_count(cli_environment) == 10
    again = runner.invoke(app, ["dev", "seed"])
    assert again.exit_code == 1
    assert "already seeded" in again.output


def test_dev_seed_command_refuses_production(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "production")
    monkeypatch.setenv("ENVMANAGER_CSRF_SECRET", "x" * 40)
    get_settings.cache_clear()
    result = runner.invoke(app, ["dev", "seed"])
    assert result.exit_code == 1
    assert "only allowed" in result.output
    assert not (cli_environment / "envmanager.db").exists()


async def test_seeded_second_factor_user_accepts_the_dev_secret(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService
) -> None:
    """The totp user has the second factor on and its code is derived from the dev secret."""
    async with create_session_factory(seeded_database)() as db:
        user = (
            await db.execute(select(User).where(User.name == seed_data.users.totp_user.name))
        ).scalar_one()
        assert user.totp_enabled and user.totp_enabled_at is not None
        assert totp.verify_code(user, pyotp.TOTP(DEV_TOTP_SECRET).now(), crypto)
        assert not totp.verify_code(user, "000000", crypto)


def test_dev_seed_command_adds_the_second_factor_user_to_an_older_database(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding again refuses, yet the second factor user appears when it was missing."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0

    async def remove_totp_user() -> None:
        engine = create_engine(cli_environment / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                await db.execute(delete(User).where(User.name == "totp_user"))
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(remove_totp_user())
    assert _user_count(cli_environment) == 9
    again = runner.invoke(app, ["dev", "seed"])
    assert again.exit_code == 1 and "already seeded" in again.output
    assert _user_count(cli_environment) == 10


def test_reset_lockouts_clears_counters_and_refuses_production(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dev helper clears counters and replay marks, and only runs in development or testing."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    get_settings.cache_clear()

    async def add_counter() -> None:
        engine = create_engine(cli_environment / "envmanager.db")
        try:
            await migrate_database(
                engine, cli_environment / "envmanager.db", cli_environment / "backups"
            )
            async with create_session_factory(engine)() as db:
                user = make_user()
                user.totp_last_step = 5
                db.add(user)
                db.add(
                    LoginCounter(
                        kind=LoginCounterKind.IP,
                        subject="203.0.113.7",
                        failures=19,
                        window_started_at=utc_now(),
                    )
                )
                await db.commit()
        finally:
            await engine.dispose()

    async def counters() -> int:
        engine = create_engine(cli_environment / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                return (
                    await db.execute(select(func.count()).select_from(LoginCounter))
                ).scalar_one()
        finally:
            await engine.dispose()

    async def last_steps() -> list[int | None]:
        engine = create_engine(cli_environment / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                return list((await db.execute(select(User.totp_last_step))).scalars())
        finally:
            await engine.dispose()

    cli_environment.mkdir(parents=True, exist_ok=True)
    asyncio.run(add_counter())
    assert asyncio.run(counters()) == 1
    assert asyncio.run(last_steps()) == [5]
    result = runner.invoke(app, ["dev", "reset-lockouts"])
    assert result.exit_code == 0 and "cleared 1 login counters" in result.output
    assert asyncio.run(counters()) == 0
    assert asyncio.run(last_steps()) == [None]

    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "production")
    monkeypatch.setenv("ENVMANAGER_CSRF_SECRET", "x" * 40)
    get_settings.cache_clear()
    refused = runner.invoke(app, ["dev", "reset-lockouts"])
    assert refused.exit_code == 1 and "only allowed" in refused.output


async def test_activity_is_added_to_an_older_seeded_database_once(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Deploy runs and the restricted token are created when missing and never duplicated."""
    async with create_session_factory(seeded_database)() as db:
        await db.execute(delete(DeployRun))
        await db.execute(delete(ApiToken))
        await db.commit()
        for _ in range(2):
            await ensure_activity(db)
            await db.commit()
        runs = (await db.execute(select(func.count()).select_from(DeployRun))).scalar_one()
        tokens = (await db.execute(select(func.count()).select_from(ApiToken))).scalar_one()
        restrictions = (
            await db.execute(select(func.count()).select_from(ApiTokenRestriction))
        ).scalar_one()
    assert (runs, tokens, restrictions) == (3, 1, 1)


async def test_audit_history_is_added_once_with_scope_ids(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Deploy and file save entries carry the ids of their item and are never duplicated."""
    async with create_session_factory(seeded_database)() as db:
        await db.execute(delete(AuditEntry))
        await db.commit()
        for _ in range(2):
            await ensure_activity(db)
            await db.commit()
        entries = (await db.execute(select(AuditEntry).order_by(AuditEntry.id))).scalars().all()
    assert [(entry.action, entry.target_name) for entry in entries] == [
        ("deploy", "web"),
        ("deploy", "mysql"),
        ("file_save", ".env.db"),
        ("file_save", ".env.api"),
    ]
    file_save = entries[2]
    assert file_save.project_id == seed_data.projects.fancy.id
    assert file_save.deployment_id == seed_data.deployments.fancy_prod.id
    assert file_save.service_id == seed_data.services.mysql.id
    assert file_save.environment_id == seed_data.envs.db.id


def _allowed_paths(data_path: Path) -> list[str]:
    """Allowed path entries in the CLI's database, sorted."""

    async def read() -> list[str]:
        engine = create_engine(data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                rows = await db.execute(select(AllowedPath.path).order_by(AllowedPath.path))
                return list(rows.scalars())
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_dev_seed_allows_the_mounted_deployments_directory_once(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command adds the directory the dev stack mounts, also to an already seeded database."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "development")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    assert DEV_ALLOWED_ROOT in _allowed_paths(cli_environment)

    async def forget() -> None:
        engine = create_engine(cli_environment / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                await db.execute(delete(AllowedPath).where(AllowedPath.path == DEV_ALLOWED_ROOT))
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(forget())
    assert DEV_ALLOWED_ROOT not in _allowed_paths(cli_environment)
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 1
    assert _allowed_paths(cli_environment).count(DEV_ALLOWED_ROOT) == 1
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 1
    assert _allowed_paths(cli_environment).count(DEV_ALLOWED_ROOT) == 1


async def test_service_figures_are_added_once_and_content_is_ciphertext(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService
) -> None:
    """The staging web files, runs and hook exist once, and the stored content is encrypted."""
    async with create_session_factory(seeded_database)() as db:
        before = [
            (await db.execute(select(func.count()).select_from(table))).scalar_one()
            for table in (Environment, DeployRun, DeployResult, ServiceHook)
        ]
        again = await ensure_service_figures(db, crypto)
        await db.commit()
        after = [
            (await db.execute(select(func.count()).select_from(table))).scalar_one()
            for table in (Environment, DeployRun, DeployResult, ServiceHook)
        ]
        stored = (
            (
                await db.execute(
                    select(EnvironmentVersion).where(
                        EnvironmentVersion.environment_id == seed_data.figures.tuning.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert again is not None and again.tuning.id == seed_data.figures.tuning.id
    assert before == after == [10, 5, 7, 1]
    assert [version.number for version in stored] == [1, 2]
    assert all("WORKERS" not in version.content_encrypted for version in stored)
    assert crypto.decrypt_text(stored[1].content_encrypted) == "WORKERS=4\n"


async def test_the_plain_dataset_leaves_the_audit_figures_out(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Tests that read the audit log by action find only the entries they wrote."""
    async with create_session_factory(seeded_database)() as db:
        found = (
            await db.execute(
                select(func.count()).select_from(AuditEntry).where(AuditEntry.action == BULK_ACTION)
            )
        ).scalar_one()
        total = (await db.execute(select(func.count()).select_from(AuditEntry))).scalar_one()
    assert found == 0 and total == 4


async def test_audit_figures_cover_every_kind_of_entry_and_are_added_once(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Every actor kind, scope level and result, a deleted project and a second page of entries."""
    async with create_session_factory(seeded_database)() as db:
        for _ in range(2):
            await ensure_audit_figures(db)
            await db.commit()
        entries = list((await db.execute(select(AuditEntry))).scalars())
        version_ids = set((await db.execute(select(EnvironmentVersion.id))).scalars())
    figures = entries[4:]
    assert len(figures) == 9 + BULK_COUNT
    assert {entry.actor_type for entry in figures} == set(AuditActorType)
    assert {entry.result for entry in figures} == set(AuditResult)
    assert sum(1 for entry in figures if entry.action == BULK_ACTION) == BULK_COUNT
    assert any(entry.project_id == DELETED_PROJECT_ID for entry in figures)
    assert any(entry.project_id is None and entry.action == "user.create" for entry in figures)
    assert any(entry.environment_id for entry in figures)
    assert any(entry.service_id and not entry.environment_id for entry in figures)
    assert any(entry.deployment_id and not entry.service_id for entry in figures)
    assert any(entry.project_id and not entry.deployment_id for entry in figures)
    versioned = [entry for entry in figures if entry.version_ids]
    assert versioned and all(set(entry.version_ids) <= version_ids for entry in versioned)
    assert all(entry.created_at.date() <= LAST_FIGURE_DAY for entry in figures)
    assert all(entry.created_at.date() >= FIRST_FIGURE_DAY for entry in figures)
    assert all(entry.ip_address for entry in figures if entry.actor_type is AuditActorType.API)


def _audit_figure_count(data_path: Path, *, delete_them: bool = False) -> int:
    """How many bulk audit figures the CLI database holds, optionally deleting them first."""

    async def count() -> int:
        engine = create_engine(data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                if delete_them:
                    await db.execute(delete(AuditEntry).where(AuditEntry.action == BULK_ACTION))
                    await db.commit()
                return (
                    await db.execute(
                        select(func.count())
                        .select_from(AuditEntry)
                        .where(AuditEntry.action == BULK_ACTION)
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(count())


def test_dev_seed_adds_the_audit_figures_to_a_new_and_to_an_older_database(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command creates the figures with the dataset and completes a database without them."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "development")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    assert _audit_figure_count(cli_environment) == BULK_COUNT
    assert _audit_figure_count(cli_environment, delete_them=True) == 0
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 1
    assert _audit_figure_count(cli_environment) == BULK_COUNT
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 1
    assert _audit_figure_count(cli_environment) == BULK_COUNT


async def test_the_seeded_project_is_a_display_name_and_stored_commands_name_their_service(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """The plain dataset names its first project with capitals, a dot and spaces, and no stored
    command holds text in braces for a service name."""
    async with create_session_factory(seeded_database)() as db:
        names = (await db.execute(select(Project.name).order_by(Project.id))).scalars().all()
        commands = (await db.execute(select(ServiceHook.commands))).scalars().all()
    assert names == [FANCY_PROJECT_NAME, "billing"] == ["Fancy Proj. Name", "billing"]
    assert seed_data.projects.fancy.name == FANCY_PROJECT_NAME
    assert commands == ["docker compose up -d --no-deps web"] == [STAGING_WEB_COMMAND]


async def _make_it_an_older_database(db: AsyncSession) -> None:
    """Put back the plain project name, the audit copies of it and a placeholder command."""
    await db.execute(update(Project).where(Project.name == FANCY_PROJECT_NAME).values(name="fancy"))
    await db.execute(
        update(ServiceHook).values(commands="docker compose up -d --no-deps {service_name}")
    )
    db.add(
        AuditEntry(
            action="project.rename",
            result=AuditResult.SUCCESS,
            actor_type=AuditActorType.SYSTEM,
            target_type="project",
            target_name="fancy",
        )
    )
    await db.commit()


async def test_an_older_database_gets_the_display_name_and_literal_commands_once(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """The project is renamed in place with its audit copies, the placeholder commands name their
    service, anything else is left alone, and running again changes nothing."""
    async with create_session_factory(seeded_database)() as db:
        await _make_it_an_older_database(db)
        db.add(
            ServiceHook(
                service_id=seed_data.services.api.id,
                type=HookType.POST_DEPLOY,
                commands="docker compose up -d --no-deps {service_name} extra",
            )
        )
        await db.commit()
    for _ in range(2):
        async with create_session_factory(seeded_database)() as db:
            await ensure_project_display_name(db)
            await ensure_literal_commands(db)
            await db.commit()
        async with create_session_factory(seeded_database)() as db:
            project = await db.scalar(
                select(Project).where(Project.id == seed_data.projects.fancy.id)
            )
            copies = (
                (
                    await db.execute(
                        select(AuditEntry.target_name).where(AuditEntry.target_type == "project")
                    )
                )
                .scalars()
                .all()
            )
            hooks = dict(
                (await db.execute(select(ServiceHook.service_id, ServiceHook.commands))).all()
            )
        assert project is not None and project.name == FANCY_PROJECT_NAME
        assert copies == [FANCY_PROJECT_NAME]
        assert hooks == {
            seed_data.services.staging_web.id: STAGING_WEB_COMMAND,
            seed_data.services.api.id: "docker compose up -d --no-deps {service_name} extra",
        }


async def test_the_display_name_is_not_applied_over_a_project_that_already_has_it(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    async with create_session_factory(seeded_database)() as db:
        db.add(Project(name="fancy"))
        await db.commit()
        await ensure_project_display_name(db)
        await db.commit()
        names = (await db.execute(select(Project.name).order_by(Project.id))).scalars().all()
    assert names == [FANCY_PROJECT_NAME, "billing", "fancy"]


def test_dev_seed_renames_the_project_and_fixes_the_commands_of_an_older_database(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command completes a database that still has the plain name and placeholder commands."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "development")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0

    async def older() -> None:
        engine = create_engine(cli_environment / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                await _make_it_an_older_database(db)
        finally:
            await engine.dispose()

    async def current() -> tuple[list[str], list[str]]:
        engine = create_engine(cli_environment / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                names = (await db.execute(select(Project.name).order_by(Project.id))).scalars()
                commands = (await db.execute(select(ServiceHook.commands))).scalars()
                return list(names), list(commands)
        finally:
            await engine.dispose()

    asyncio.run(older())
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 1
    names, commands = asyncio.run(current())
    assert names == [FANCY_PROJECT_NAME, "billing"]
    assert commands == [STAGING_WEB_COMMAND]
