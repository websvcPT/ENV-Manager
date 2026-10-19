"""Project service: the scoped list and superadmin create, rename and delete."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import (
    ApiToken,
    ApiTokenRestriction,
    AuditEntry,
    AuditResult,
    Deployment,
    DeployStatus,
    Project,
    User,
)
from app.models.types import utc_now
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.permissions import load_user_scope
from app.services.projects import (
    InvalidProjectNameError,
    ProjectExistsError,
    ProjectInUseError,
    ProjectRow,
    create_project,
    delete_project,
    list_project_rows,
    rename_project,
    validate_project_name,
)
from app.services.tokens import create_token, revoke_token


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


async def _rows(db: AsyncSession, user: User, query: str = "") -> dict[str, ProjectRow]:
    """The list a user sees, by project name."""
    scope = await load_user_scope(db, user)
    return {row.name: row for row in await list_project_rows(db, scope, query=query)}


async def _audit(db: AsyncSession, action: str) -> list[AuditEntry]:
    """Audit entries of an action, oldest first."""
    stmt = select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
    return list((await db.execute(stmt)).scalars())


async def test_superadmin_sees_every_project_with_counts(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Deployments, last deploy, attention and members are counted over the whole tree."""
    rows = await _rows(db, seed_data.users.superadmin)
    assert list(rows) == ["billing", "Fancy Proj. Name"]
    fancy, billing = rows["Fancy Proj. Name"], rows["billing"]
    assert (fancy.deployments, billing.deployments) == (2, 2)
    assert fancy.limited_to == ()
    assert (fancy.members, billing.members) == (6, 2)
    assert fancy.last_deploy is not None
    assert fancy.last_deploy.status is DeployStatus.FAILED
    assert fancy.last_deploy.actor == "root"
    assert fancy.attention == 1


async def test_dry_runs_do_not_count_as_a_deploy(db: AsyncSession, seed_data: SeedData) -> None:
    """A project with only a dry run reads as never deployed and needs no attention."""
    billing = (await _rows(db, seed_data.users.superadmin))["billing"]
    assert billing.last_deploy is None
    assert billing.attention == 0


async def test_non_superadmins_do_not_see_members(db: AsyncSession, seed_data: SeedData) -> None:
    """Who has access is a superadmin matter."""
    rows = await _rows(db, seed_data.users.admin)
    assert set(rows) == {"billing", "Fancy Proj. Name"}
    assert {row.members for row in rows.values()} == {None}


async def test_project_grant_shows_only_its_project(db: AsyncSession, seed_data: SeedData) -> None:
    """A project grant covers the whole project and names nothing else."""
    rows = await _rows(db, seed_data.users.viewer)
    assert list(rows) == ["Fancy Proj. Name"]
    assert rows["Fancy Proj. Name"].deployments == 2
    assert rows["Fancy Proj. Name"].limited_to == ()


async def test_deployment_grant_lists_the_project_as_limited(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The parent appears by name with what is granted, counting only the visible deployment."""
    rows = await _rows(db, seed_data.users.deployment_admin)
    assert list(rows) == ["Fancy Proj. Name"]
    assert rows["Fancy Proj. Name"].limited_to == ("prod",)
    assert rows["Fancy Proj. Name"].deployments == 1
    assert rows["Fancy Proj. Name"].attention == 1


async def test_environment_grants_name_only_granted_environments(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """An environment-scoped viewer sees both projects limited to the granted files."""
    rows = await _rows(db, seed_data.users.env_scoped_viewer)
    assert rows["Fancy Proj. Name"].limited_to == (".env.db",)
    assert rows["billing"].limited_to == (".env.api",)
    assert rows["Fancy Proj. Name"].deployments == 1


async def test_a_user_without_grants_sees_nothing(db: AsyncSession, seed_data: SeedData) -> None:
    """No grant, no projects."""
    assert await _rows(db, seed_data.users.ungranted_admin) == {}


SEARCHES = [
    ("fancy", ["Fancy Proj. Name"]),
    ("PROJ. n", ["Fancy Proj. Name"]),
    ("BILL", ["billing"]),
    ("%", []),
]


@pytest.mark.parametrize(("query", "expected"), SEARCHES)
async def test_search_filters_by_name_ignoring_case(
    db: AsyncSession, seed_data: SeedData, query: str, expected: list[str]
) -> None:
    """The query is a case-insensitive substring of the name; wildcards are literal."""
    assert list(await _rows(db, seed_data.users.superadmin, query)) == expected


BAD_NAMES = [
    "",
    "   ",
    "a" * 101,
    "ünï",
    "é",
    "名前",
    "a/b",
    "back\\slash",
    "bang!",
    "semi;colon",
    "angle<b>",
    "quote'd",
    "two\tcolumns",
    "two\nlines",
    "nul\x00byte",
    "(parens)",
    "at@sign",
    "plus+sign",
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_invalid_names_are_refused(name: str) -> None:
    """Only ASCII letters, digits, spaces and . - _ within the length limit are accepted."""
    with pytest.raises(InvalidProjectNameError):
        validate_project_name(name)


def test_a_refused_name_says_what_is_allowed() -> None:
    with pytest.raises(InvalidProjectNameError) as raised:
        validate_project_name("bang!")
    assert str(raised.value) == (
        "Use 1 to 100 characters: letters, numbers, spaces and the characters . - _"
    )
    with pytest.raises(InvalidProjectNameError, match=r"Enter a project name\."):
        validate_project_name("  ")


VALID_NAMES = [
    ("  way2rentals ", "way2rentals"),
    ("a-1", "a-1"),
    ("Fancy Proj. Name", "Fancy Proj. Name"),
    ("UPPER lower 123", "UPPER lower 123"),
    ("  under_score.dot-dash  ", "under_score.dot-dash"),
    ("-leading", "-leading"),
    ("7", "7"),
    ("a" * 100, "a" * 100),
    (" " + "a" * 100 + " ", "a" * 100),
]


@pytest.mark.parametrize(("typed", "stored"), VALID_NAMES)
def test_valid_names_are_display_names_and_trimmed(typed: str, stored: str) -> None:
    """Letters in any case, digits, spaces and . - _ are kept as typed; the ends are trimmed."""
    assert validate_project_name(typed) == stored


async def test_create_project_is_audited(db: AsyncSession, seed_data: SeedData) -> None:
    """A new project is stored and audited with its scope id."""
    project = await create_project(
        db, name="fleetops", actor=seed_data.users.superadmin, ip_address="10.0.0.5"
    )
    (entry,) = await _audit(db, "project.create")
    assert (entry.result, entry.target_name, entry.project_id) == (
        AuditResult.SUCCESS,
        "fleetops",
        project.id,
    )
    assert (entry.actor_name, entry.ip_address) == ("root", "10.0.0.5")


async def test_a_display_name_is_stored_as_typed(db: AsyncSession, seed_data: SeedData) -> None:
    project = await create_project(
        db, name="  Fleet Ops. v2_beta-1 ", actor=seed_data.users.superadmin
    )
    assert project.name == "Fleet Ops. v2_beta-1"
    (entry,) = await _audit(db, "project.create")
    assert entry.target_name == "Fleet Ops. v2_beta-1"


@pytest.mark.parametrize(
    "name", ["Fancy Proj. Name", "fancy proj. name", "FANCY PROJ. NAME", "  fAnCy PROJ. name  "]
)
async def test_duplicate_name_is_refused_whatever_the_case(
    db: AsyncSession, seed_data: SeedData, name: str
) -> None:
    """A second project cannot take an existing name in any case, and nothing is audited."""
    with pytest.raises(ProjectExistsError):
        await create_project(db, name=name, actor=seed_data.users.superadmin)
    assert await _audit(db, "project.create") == []


async def test_a_name_taken_in_another_case_while_the_page_was_open_is_still_refused(
    db: AsyncSession, seed_data: SeedData, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When two creations race past the check, the unique index decides and the answer is the same.

    The check is skipped here to stand for the loser of the race; the session stays usable.
    """

    async def never_taken(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("app.services.projects._ensure_name_free", never_taken)
    with pytest.raises(ProjectExistsError, match="already exists"):
        await create_project(db, name="FANCY proj. NAME", actor=seed_data.users.superadmin)
    billing = await db.get(Project, seed_data.projects.billing.id)
    assert billing is not None
    with pytest.raises(ProjectExistsError):
        await rename_project(db, billing, name="fancy PROJ. name", actor=seed_data.users.superadmin)
    names = (await db.execute(select(Project.name).order_by(func.lower(Project.name)))).scalars()
    assert list(names) == ["billing", "Fancy Proj. Name"]
    assert await _audit(db, "project.create") == []
    assert await _audit(db, "project.rename") == []


async def test_a_project_can_change_only_the_case_of_its_own_name(
    db: AsyncSession, seed_data: SeedData
) -> None:
    billing = await db.get(Project, seed_data.projects.billing.id)
    assert billing is not None
    renamed = await rename_project(db, billing, name="BILLING", actor=seed_data.users.superadmin)
    assert (renamed.id, renamed.name) == (billing.id, "BILLING")
    stored_name = await db.scalar(select(Project.name).where(Project.id == billing.id))
    assert stored_name == "BILLING"
    (entry,) = await _audit(db, "project.rename")
    assert entry.details == {"previous_name": "billing"}
    with pytest.raises(ProjectExistsError):
        await rename_project(db, renamed, name="FANCY proj. name", actor=seed_data.users.superadmin)


async def test_projects_are_listed_by_name_ignoring_case(
    db: AsyncSession, seed_data: SeedData
) -> None:
    for name in ("Zeta", "alpha", "Beta"):
        await create_project(db, name=name, actor=seed_data.users.superadmin)
    assert list(await _rows(db, seed_data.users.superadmin)) == [
        "alpha",
        "Beta",
        "billing",
        "Fancy Proj. Name",
        "Zeta",
    ]


async def test_rename_project_audits_the_previous_name(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Renaming keeps the id, records the old name and refuses a taken name."""
    project = seed_data.projects.billing
    renamed = await rename_project(db, project, name="invoicing", actor=seed_data.users.superadmin)
    assert (renamed.id, renamed.name) == (project.id, "invoicing")
    (entry,) = await _audit(db, "project.rename")
    assert entry.details == {"previous_name": "billing"}
    with pytest.raises(ProjectExistsError):
        await rename_project(db, renamed, name="Fancy Proj. Name", actor=seed_data.users.superadmin)
    await rename_project(db, renamed, name="invoicing", actor=seed_data.users.superadmin)
    assert len(await _audit(db, "project.rename")) == 1


async def test_delete_project_removes_the_tree_and_keeps_history(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Deleting cascades to the structure and leaves an audit entry with the project id."""
    project = seed_data.projects.fancy
    await delete_project(db, project, actor=seed_data.users.superadmin)
    remaining = (await db.execute(select(Project.name))).scalars().all()
    assert remaining == ["billing"]
    deployments = (await db.execute(select(func.count()).select_from(Deployment))).scalar_one()
    assert deployments == 2
    (entry,) = await _audit(db, "project.delete")
    assert (entry.result, entry.project_id, entry.details) == (
        AuditResult.SUCCESS,
        project.id,
        {"deployments": 2},
    )


async def test_delete_is_refused_while_a_token_is_restricted_inside(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Removing the restriction would widen the token, so the project stays and it is audited."""
    with pytest.raises(ProjectInUseError, match="billing-ci"):
        await delete_project(db, seed_data.projects.billing, actor=seed_data.users.superadmin)
    names = (
        (await db.execute(select(Project.name).order_by(func.lower(Project.name)))).scalars().all()
    )
    assert names == ["billing", "Fancy Proj. Name"]
    (entry,) = await _audit(db, "project.delete")
    assert entry.result is AuditResult.BLOCKED
    assert entry.details == {"reason": "restricted_tokens", "tokens": ["billing-ci"]}


async def test_deleted_ids_are_never_reused(db: AsyncSession, seed_data: SeedData) -> None:
    """The newest project id is not handed out again, so old audit scope ids stay unambiguous."""
    created = await create_project(db, name="temp", actor=seed_data.users.superadmin)
    await delete_project(db, created, actor=seed_data.users.superadmin)
    again = await create_project(db, name="temp", actor=seed_data.users.superadmin)
    assert again.id > created.id


async def test_a_revoked_token_no_longer_holds_its_project(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A revoked token can never be used again, so deleting the project removes its restriction."""
    token = seed_data.tokens[0]
    token_row = await db.get(ApiToken, token.id)
    assert token_row is not None
    await revoke_token(db, token_row, AuditActor.for_user(seed_data.users.admin))
    await delete_project(db, seed_data.projects.billing, actor=seed_data.users.superadmin)
    names = (await db.execute(select(Project.name))).scalars().all()
    assert names == ["Fancy Proj. Name"]
    assert (await db.execute(select(ApiTokenRestriction))).first() is None
    kept = await db.get(ApiToken, token.id)
    assert kept is not None and kept.revoked_at is not None
    (entry,) = await _audit(db, "project.delete")
    assert entry.result is AuditResult.SUCCESS


async def test_an_expired_token_no_longer_holds_its_project(
    db: AsyncSession, seed_data: SeedData
) -> None:
    token_row = await db.get(ApiToken, seed_data.tokens[0].id)
    assert token_row is not None
    token_row.expires_at = utc_now() - timedelta(minutes=1)
    await db.commit()
    await delete_project(db, seed_data.projects.billing, actor=seed_data.users.superadmin)
    assert (await db.execute(select(ApiTokenRestriction))).first() is None


async def test_only_the_dead_tokens_restrictions_go_with_the_project(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A live token restricted elsewhere keeps its restriction; one inside blocks the delete."""
    admin = seed_data.users.admin
    scope = await load_user_scope(db, admin)
    kept = await create_token(
        db,
        admin,
        scope,
        name="fancy-ci",
        operations=["deploy"],
        restriction=f"project:{seed_data.projects.fancy.id}",
        expires="",
        actor=AuditActor.for_user(admin),
    )
    seeded = await db.get(ApiToken, seed_data.tokens[0].id)
    assert seeded is not None
    await revoke_token(db, seeded, AuditActor.for_user(admin))
    await delete_project(db, seed_data.projects.billing, actor=seed_data.users.superadmin)
    rows = (await db.execute(select(ApiTokenRestriction))).scalars().all()
    assert [row.token_id for row in rows] == [kept.token.id]
    with pytest.raises(ProjectInUseError, match="fancy-ci"):
        await delete_project(db, seed_data.projects.fancy, actor=seed_data.users.superadmin)
