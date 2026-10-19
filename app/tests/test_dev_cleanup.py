"""The development cleanup of scratch users and projects: the rule, the cascades and the command."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from app.cli.main import app
from app.config import get_settings
from app.core.db import create_engine, create_session_factory
from app.models import (
    AccessGrant,
    ApiToken,
    ApiTokenRestriction,
    AuditEntry,
    AuditResult,
    Base,
    Deployment,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Environment,
    EnvironmentVersion,
    GrantLevel,
    Project,
    Service,
    User,
    UserGroup,
    UserSession,
)
from app.models.types import utc_now
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.dev_cleanup import MIN_SCRATCH_AGE, remove_scratch_data
from app.services.permissions import load_user_scope
from app.services.tokens import create_token
from app.tests.audit_helpers import audit_entries
from app.tests.auth_helpers import make_user
from app.tests.backup_helpers import read_rows
from app.tests.template_databases import SeededTemplate, copy_database

runner = CliRunner()
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
CUTOFF = NOW - MIN_SCRATCH_AGE
OLD = NOW - timedelta(hours=3)
RECENT = NOW - timedelta(minutes=5)


def scratch_user(
    name: str,
    created: datetime,
    *,
    active: bool = True,
    group: UserGroup = UserGroup.VIEWER,
    email: str | None = None,
) -> User:
    """A user created at the given moment, whose email is ``<name>@example.test`` by default."""
    user = make_user(name, group, active=active, email=email)
    user.created_at = created
    return user


async def add_project(db: AsyncSession, name: str, created: datetime) -> Project:
    """A project created at the given moment with one deployment, service and file."""
    project = Project(name=name, created_at=created)
    db.add(project)
    await db.flush()
    deployment = Deployment(project_id=project.id, name="prod", root_path="/srv/scratch")
    db.add(deployment)
    await db.flush()
    service = Service(deployment_id=deployment.id, name="web")
    db.add(service)
    await db.flush()
    db.add(Environment(service_id=service.id, name=".env", target_path=".env"))
    await db.commit()
    return project


async def grant_token(db: AsyncSession, owner: User, name: str, project: Project) -> ApiToken:
    """A token of a superadmin restricted to the project."""
    created = await create_token(
        db,
        owner,
        await load_user_scope(db, owner),
        name=name,
        operations=["deploy"],
        restriction=f"project:{project.id}",
        expires="",
        actor=AuditActor.for_user(owner),
    )
    return created.token


async def user_names(db: AsyncSession) -> set[str]:
    """The names of every user."""
    return set((await db.execute(select(User.name))).scalars())


async def project_names(db: AsyncSession) -> set[str]:
    """The names of every project."""
    return set((await db.execute(select(Project.name))).scalars())


async def count(db: AsyncSession, model: type[Base]) -> int:
    """The number of rows of a model."""
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def test_only_old_users_with_the_email_prefix_are_removed(db: AsyncSession) -> None:
    """The prefix is on the email and the age decides, whether the user is active or disabled."""
    seeded = await user_names(db)
    removed = [
        scratch_user("e2e-u-1-aaaaa-desktop", OLD),
        scratch_user("e2e-u-2-bbbbb-desktop", OLD, active=False),
        scratch_user("shouting", OLD, email="E2E-U-3-CCCCC@Example.test"),
        scratch_user("e2e-u-4-ddddd-phone", CUTOFF - timedelta(seconds=1)),
    ]
    recent = [
        scratch_user("e2e-u-5-eeeee-phone", RECENT),
        scratch_user("e2e-u-6-fffff-phone", RECENT, active=False),
        scratch_user("e2e-u-7-ggggg-phone", CUTOFF),
    ]
    near_misses = [
        scratch_user("e2e-u-named-only", OLD, email="named-only@example.test"),
        scratch_user("e2e-root", OLD),
        scratch_user("e2e-user-a", OLD),
        scratch_user("e2e-shots-1", OLD),
        scratch_user("x-e2e-u-inside", OLD),
        scratch_user("e2e", OLD, email="e2e-u@example.test"),
    ]
    db.add_all([*removed, *recent, *near_misses])
    await db.commit()

    result = await remove_scratch_data(db, now=NOW)

    survivors = {user.name for user in [*recent, *near_misses]}
    assert await user_names(db) == seeded | survivors
    assert (result.users_removed, result.users_recent) == (len(removed), len(recent))
    assert (result.projects_removed, result.projects_recent, result.projects_in_use) == (0, 0, ())


async def test_a_removed_user_takes_its_access_with_it_and_leaves_the_history(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Sessions, grants and tokens with their restrictions go; entries, runs and authors stay."""
    billing = seed_data.projects.billing
    name = "e2e-u-9-hhhhh-desktop"
    scratch = scratch_user(name, OLD, group=UserGroup.SUPERADMIN)
    db.add(scratch)
    await db.flush()
    scratch_id = scratch.id
    db.add(UserSession(token_hash="a" * 64, user_id=scratch_id, expires_at=NOW))
    db.add(AccessGrant(user_id=scratch_id, level=GrantLevel.PROJECT, project_id=billing.id))
    token = await grant_token(db, scratch, "scratch-ci", billing)
    token_id = token.id
    db.add(
        EnvironmentVersion(
            environment_id=seed_data.envs.db.id,
            number=99,
            content_encrypted="sealed",
            content_hash="hash",
            author_id=scratch_id,
        )
    )
    db.add(
        DeployRun(
            service_name="scratch-service",
            deployment_name="prod",
            triggered_by_user_id=scratch_id,
            trigger=DeployTrigger.UI,
            status=DeployStatus.SUCCESS,
        )
    )
    await db.commit()
    runs, versions = await count(db, DeployRun), await count(db, EnvironmentVersion)
    by_scratch = select(AuditEntry).where(AuditEntry.actor_user_id == scratch_id)
    assert [entry.action for entry in (await db.execute(by_scratch)).scalars()] == ["token.create"]

    result = await remove_scratch_data(db, now=NOW)

    db.expire_all()
    assert result.users_removed == 1
    assert name not in await user_names(db)
    sessions = await db.execute(select(UserSession).where(UserSession.user_id == scratch_id))
    grants = await db.execute(select(AccessGrant).where(AccessGrant.user_id == scratch_id))
    restrictions = await db.execute(
        select(ApiTokenRestriction).where(ApiTokenRestriction.token_id == token_id)
    )
    assert (sessions.first(), grants.first(), restrictions.first()) == (None, None, None)
    assert await db.get(ApiToken, token_id) is None
    (entry,) = (
        (await db.execute(select(AuditEntry).where(AuditEntry.actor_name == name))).scalars().all()
    )
    assert (entry.action, entry.actor_user_id) == ("token.create", None)
    assert (await count(db, DeployRun), await count(db, EnvironmentVersion)) == (runs, versions)
    author = await db.execute(
        select(EnvironmentVersion.author_id).where(EnvironmentVersion.number == 99)
    )
    triggered_by = await db.execute(
        select(DeployRun.triggered_by_user_id).where(DeployRun.service_name == "scratch-service")
    )
    assert (author.scalar_one(), triggered_by.scalar_one()) == (None, None)


async def test_only_old_projects_with_the_name_prefix_are_removed(db: AsyncSession) -> None:
    """A removed project takes its tree with it and is audited as the command line."""
    seeded = await project_names(db)
    for name, created in (
        ("e2e-1-old", OLD),
        ("E2E-detail-2-old", OLD),
        ("e2e-3-recent", RECENT),
        ("e2e-4-edge", CUTOFF),
        ("e2e 5 no hyphen", OLD),
        ("my e2e-project", OLD),
        ("e2e", OLD),
    ):
        await add_project(db, name, created)
    before = [await count(db, model) for model in (Deployment, Service, Environment)]

    result = await remove_scratch_data(db, now=NOW)

    assert await project_names(db) == seeded | {
        "e2e-3-recent",
        "e2e-4-edge",
        "e2e 5 no hyphen",
        "my e2e-project",
        "e2e",
    }
    assert (result.projects_removed, result.projects_recent) == (2, 2)
    after = [await count(db, model) for model in (Deployment, Service, Environment)]
    assert after == [total - 2 for total in before]
    entries = await audit_entries(db, "project.delete")
    assert [(entry.actor_type.value, entry.actor_name, entry.result) for entry in entries] == [
        ("cli", "cli", AuditResult.SUCCESS)
    ] * 2
    assert {entry.target_name for entry in entries} == {"e2e-1-old", "E2E-detail-2-old"}


async def test_a_scratch_project_with_a_live_token_of_a_surviving_user_is_kept(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The restricted token guard of the project delete applies, and the refusal is named."""
    guarded = await add_project(db, "e2e-10-guarded", OLD)
    free = await add_project(db, "e2e-11-free", OLD)
    await grant_token(db, seed_data.users.superadmin, "guard-ci", guarded)

    result = await remove_scratch_data(db, now=NOW)

    names = await project_names(db)
    assert guarded.name in names and free.name not in names
    assert (result.projects_removed, result.projects_in_use) == (1, ("e2e-10-guarded",))
    blocked = [
        entry
        for entry in await audit_entries(db, "project.delete")
        if entry.result is AuditResult.BLOCKED
    ]
    assert [(entry.actor_name, entry.target_name) for entry in blocked] == [
        ("cli", "e2e-10-guarded")
    ]
    assert blocked[0].details == {"reason": "restricted_tokens", "tokens": ["guard-ci"]}


async def test_a_token_of_a_removed_user_no_longer_guards_a_scratch_project(
    db: AsyncSession,
) -> None:
    """Users go first, so a scratch user's token never keeps a scratch project alive."""
    seeded_restrictions = await count(db, ApiTokenRestriction)
    scratch = scratch_user("e2e-u-12-iiiii-phone", OLD, group=UserGroup.SUPERADMIN)
    db.add(scratch)
    await db.flush()
    project = await add_project(db, "e2e-12-held", OLD)
    await grant_token(db, scratch, "scratch-ci", project)
    assert await count(db, ApiTokenRestriction) == seeded_restrictions + 1

    result = await remove_scratch_data(db, now=NOW)

    assert (result.users_removed, result.projects_removed, result.projects_in_use) == (1, 1, ())
    assert "e2e-12-held" not in await project_names(db)
    assert await count(db, ApiTokenRestriction) == seeded_restrictions


async def test_the_age_is_a_parameter(db: AsyncSession) -> None:
    """Nothing newer than the age goes, whatever the default is."""
    db.add(scratch_user("e2e-u-13-jjjjj-phone", NOW - timedelta(minutes=10)))
    await db.commit()
    assert (await remove_scratch_data(db, now=NOW)).users_removed == 0
    younger = await remove_scratch_data(db, now=NOW, min_age=timedelta(minutes=5))
    assert younger.users_removed == 1


@pytest.fixture
def data_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded_template: SeededTemplate
) -> Iterator[Path]:
    """A data folder holding a copy of the seeded database that the command line is pointed at."""
    folder = tmp_path / "data"
    copy_database(seeded_template.path, folder / "envmanager.db")
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(folder))
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    get_settings.cache_clear()
    yield folder
    get_settings.cache_clear()


def populate(folder: Path, build: Callable[[AsyncSession], Awaitable[None]]) -> None:
    """Run a coroutine function with a session on the command line's database."""

    async def go() -> None:
        engine = create_engine(folder / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                await build(db)
        finally:
            await engine.dispose()

    asyncio.run(go())


def stored(folder: Path, table: Literal["users", "projects"]) -> set[str]:
    """The names in the users or projects table of the command line's database."""
    queries = {"users": "SELECT name FROM users", "projects": "SELECT name FROM projects"}
    return {row[0] for row in read_rows(folder / "envmanager.db", queries[table])}


def project_deletes(folder: Path) -> list[tuple[str, str]]:
    """The actor type and name of every project delete entry in the command line's database."""
    return [
        (row[0], row[1])
        for row in read_rows(
            folder / "envmanager.db",
            "SELECT actor_type, actor_name FROM audit_entries WHERE action = 'project.delete'",
        )
    ]


@pytest.mark.parametrize("environment", ["testing", "development"])
def test_the_command_removes_old_scratch_rows_and_prints_the_counts(
    data_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """On the seeded database: seeded users and projects, near misses and new rows survive."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", environment)
    get_settings.cache_clear()
    seeded_users, seeded_projects = stored(data_path, "users"), stored(data_path, "projects")
    deletes_before = project_deletes(data_path)
    now = utc_now()

    async def build(db: AsyncSession) -> None:
        db.add_all(
            [
                scratch_user("e2e-u-1-aaaaa-desktop", now - timedelta(hours=2)),
                scratch_user("e2e-u-2-bbbbb-phone", now - timedelta(hours=2), active=False),
                scratch_user("e2e-u-3-ccccc-phone", now - timedelta(minutes=3)),
                scratch_user("e2e-root", now - timedelta(hours=2)),
            ]
        )
        await db.commit()
        await add_project(db, "e2e-1-old", now - timedelta(hours=2))
        await add_project(db, "e2e-2-recent", now - timedelta(minutes=3))
        await add_project(db, "e2e", now - timedelta(hours=2))

    populate(data_path, build)

    result = runner.invoke(app, ["dev", "cleanup"])

    assert result.exit_code == 0, result.output
    assert "removed 2 scratch users and 1 scratch projects" in result.output
    assert "kept 1 scratch users and 1 scratch projects created in the last 60 minutes" in (
        result.output
    )
    assert stored(data_path, "users") == seeded_users | {"e2e-u-3-ccccc-phone", "e2e-root"}
    assert stored(data_path, "projects") == seeded_projects | {"e2e-2-recent", "e2e"}
    assert project_deletes(data_path) == [*deletes_before, ("cli", "cli")]


def test_the_command_names_a_project_that_a_restricted_token_keeps(data_path: Path) -> None:
    """A refused project is not an error: the rest is cleaned and the refusal is printed."""
    seeded = stored(data_path, "projects")

    async def build(db: AsyncSession) -> None:
        superadmin = (
            (await db.execute(select(User).where(User.group == UserGroup.SUPERADMIN)))
            .scalars()
            .first()
        )
        assert superadmin is not None
        project = await add_project(db, "e2e-5-guarded", utc_now() - timedelta(hours=2))
        await grant_token(db, superadmin, "guard-ci", project)

    populate(data_path, build)

    result = runner.invoke(app, ["dev", "cleanup"])

    assert result.exit_code == 0, result.output
    assert "kept 1 scratch projects with restricted API tokens: e2e-5-guarded" in result.output
    assert stored(data_path, "projects") == seeded | {"e2e-5-guarded"}


def test_the_command_is_refused_in_production(
    data_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is removed outside development and testing, however old the rows are."""

    async def build(db: AsyncSession) -> None:
        db.add(scratch_user("e2e-u-1-aaaaa-desktop", utc_now() - timedelta(hours=2)))
        await db.commit()
        await add_project(db, "e2e-1-old", utc_now() - timedelta(hours=2))

    populate(data_path, build)
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "production")
    monkeypatch.setenv("ENVMANAGER_CSRF_SECRET", "x" * 40)
    get_settings.cache_clear()

    result = runner.invoke(app, ["dev", "cleanup"])

    assert result.exit_code == 1
    assert "only allowed" in result.output
    assert "e2e-u-1-aaaaa-desktop" in stored(data_path, "users")
    assert "e2e-1-old" in stored(data_path, "projects")
