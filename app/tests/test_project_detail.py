"""Project detail service: header figures, deployment rows, activity and members by scope."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import DeployStatus, User, UserGroup
from app.seeds import SeedData
from app.services.permissions import load_user_scope
from app.services.project_detail import (
    ProjectDetail,
    list_activity,
    list_members,
    load_project_detail,
)


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


async def _detail(db: AsyncSession, user: User, project_id: int) -> ProjectDetail | None:
    """The project as a user sees it."""
    return await load_project_detail(db, await load_user_scope(db, user), project_id)


async def test_superadmin_sees_every_deployment_with_state(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Rows carry root path, file count, last deploy and the non-success flags of the services."""
    detail = await _detail(db, seed_data.users.superadmin, seed_data.projects.rentals.id)
    assert detail is not None and not detail.partial
    assert [row.name for row in detail.deployments] == ["prod", "staging"]
    prod, staging = detail.deployments
    assert prod.root_path == "/opt/rentals/prod"
    assert prod.files == 3
    assert prod.last_deploy is not None
    assert prod.last_deploy.status is DeployStatus.FAILED
    assert prod.last_deploy.actor == "root"
    assert prod.flags == (DeployStatus.FAILED,)
    assert staging.files == 4
    assert staging.last_deploy is not None
    assert staging.last_deploy.status is DeployStatus.SUCCESS
    assert staging.flags == ()
    assert detail.attention == 1
    assert detail.members == 6
    assert detail.limited_to == ()


async def test_members_are_only_counted_for_the_superadmin(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Who has access is not revealed to other groups."""
    detail = await _detail(db, seed_data.users.admin, seed_data.projects.rentals.id)
    assert detail is not None
    assert detail.members is None


async def test_deployment_grant_lists_the_granted_deployment_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A deployment grant shows the project as partial with just that deployment and its path."""
    detail = await _detail(db, seed_data.users.deployment_admin, seed_data.projects.rentals.id)
    assert detail is not None and detail.partial
    assert [row.name for row in detail.deployments] == ["prod"]
    assert detail.deployments[0].root_path == "/opt/rentals/prod"
    assert detail.limited_to == ("prod",)
    assert detail.all_granted_are_deployments


async def test_environment_grant_shows_the_parent_by_name_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A deployment reached only as the parent of a granted environment hides its path."""
    detail = await _detail(db, seed_data.users.env_scoped_admin, seed_data.projects.rentals.id)
    assert detail is not None and detail.partial
    assert [row.name for row in detail.deployments] == ["prod"]
    row = detail.deployments[0]
    assert row.root_path is None
    assert row.files == 1
    assert detail.limited_to == (".env.db",)
    assert not detail.all_granted_are_deployments


async def test_project_outside_the_scope_is_not_found(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A project the caller cannot reach (or that does not exist) loads as None."""
    billing = seed_data.projects.billing.id
    assert await _detail(db, seed_data.users.viewer, billing) is None
    assert await _detail(db, seed_data.users.ungranted_admin, billing) is None
    assert await _detail(db, seed_data.users.superadmin, 9999) is None


async def test_activity_is_limited_to_the_scope_of_the_caller(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Scoped users see only the audit entries of their deployment or environment."""
    rentals = seed_data.projects.rentals.id

    async def actions(user: User) -> list[tuple[str, str]]:
        scope = await load_user_scope(db, user)
        return [(row.action, row.target) for row in await list_activity(db, scope, rentals)]

    everything = await actions(seed_data.users.superadmin)
    assert everything == [
        ("deploy", "mysql"),
        ("deploy", "web"),
        ("file_save", ".env.db"),
    ]
    assert await actions(seed_data.users.project_admin) == everything
    assert await actions(seed_data.users.deployment_admin) == everything
    assert await actions(seed_data.users.env_scoped_admin) == [("file_save", ".env.db")]
    assert await actions(seed_data.users.ungranted_admin) == []


async def test_activity_respects_the_limit(db: AsyncSession, seed_data: SeedData) -> None:
    """Only the newest entries are returned."""
    scope = await load_user_scope(db, seed_data.users.superadmin)
    rows = await list_activity(db, scope, seed_data.projects.rentals.id, limit=1)
    assert [(row.action, row.target) for row in rows] == [("deploy", "mysql")]


async def test_members_show_direct_inherited_and_group_access(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Project grants are direct, deeper grants name their source, superadmins come by group."""
    members = {m.name: m for m in await list_members(db, seed_data.projects.rentals.id)}
    assert set(members) == {
        "root",
        "admin",
        "viewer",
        "project_admin",
        "deployment_admin",
        "env_scoped_admin",
        "env_scoped_viewer",
    }
    assert members["viewer"].direct and members["viewer"].group is UserGroup.VIEWER
    assert members["deployment_admin"].sources == (("prod", "Deployment"),)
    assert members["env_scoped_viewer"].sources == ((".env.db", "Environment"),)
    assert not members["root"].direct and members["root"].sources == ()
    billing = {m.name for m in await list_members(db, seed_data.projects.billing.id)}
    assert billing == {"root", "admin", "env_scoped_viewer"}


async def test_disabled_users_are_not_members(db: AsyncSession, seed_data: SeedData) -> None:
    """Only active users are listed."""
    seed_data.users.viewer.is_active = False
    await db.merge(seed_data.users.viewer)
    await db.commit()
    names = {m.name for m in await list_members(db, seed_data.projects.rentals.id)}
    assert "viewer" not in names
