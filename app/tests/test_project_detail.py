"""Project detail service: header figures, deployment rows and activity by scope."""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AllowedPath, DeployStatus, User
from app.seeds import SeedData
from app.services.paths import load_allowed_paths
from app.services.permissions import load_user_scope
from app.services.project_detail import ProjectDetail, list_activity, load_project_detail


async def _detail(db: AsyncSession, user: User, project_id: int) -> ProjectDetail | None:
    """The project as a user sees it."""
    scope = await load_user_scope(db, user)
    return await load_project_detail(db, scope, project_id, await load_allowed_paths(db))


async def test_superadmin_sees_every_deployment_with_state(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Rows carry root path, file count, last deploy and the non-success flags of the services."""
    detail = await _detail(db, seed_data.users.superadmin, seed_data.projects.fancy.id)
    assert detail is not None and not detail.partial
    assert [row.name for row in detail.deployments] == ["prod", "staging"]
    prod, staging = detail.deployments
    assert prod.root_path == "/opt/fancy/prod"
    assert prod.files == 4
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
    detail = await _detail(db, seed_data.users.admin, seed_data.projects.fancy.id)
    assert detail is not None
    assert detail.members is None


async def test_deployment_grant_lists_the_granted_deployment_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A deployment grant shows the project as partial with just that deployment and its path."""
    detail = await _detail(db, seed_data.users.deployment_admin, seed_data.projects.fancy.id)
    assert detail is not None and detail.partial
    assert [row.name for row in detail.deployments] == ["prod"]
    assert detail.deployments[0].root_path == "/opt/fancy/prod"
    assert detail.limited_to == ("prod",)
    assert detail.all_granted_are_deployments


async def test_environment_grant_shows_the_parent_by_name_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A deployment reached only as the parent of a granted environment hides its path."""
    detail = await _detail(db, seed_data.users.env_scoped_admin, seed_data.projects.fancy.id)
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
    fancy = seed_data.projects.fancy.id

    async def actions(user: User) -> list[tuple[str, str]]:
        scope = await load_user_scope(db, user)
        return [(row.action, row.target) for row in await list_activity(db, scope, fancy)]

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
    rows = await list_activity(db, scope, seed_data.projects.fancy.id, limit=1)
    assert [(row.action, row.target) for row in rows] == [("deploy", "mysql")]


async def test_check_dist_row_action_follows_group_files_and_allowed_paths(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Checkers see it on deployments with files inside the allowed paths; viewers never."""
    users, fancy = seed_data.users, seed_data.projects.fancy.id
    for user in (users.superadmin, users.admin, users.deployment_admin):
        detail = await _detail(db, user, fancy)
        assert detail is not None
        assert all(row.can_check_dist for row in detail.deployments), user.name
    scoped = await _detail(db, users.env_scoped_admin, fancy)
    assert scoped is not None and [row.can_check_dist for row in scoped.deployments] == [True]
    viewer = await _detail(db, users.viewer, fancy)
    assert viewer is not None and not any(row.can_check_dist for row in viewer.deployments)
    billing = await _detail(db, users.superadmin, seed_data.projects.billing.id)
    assert billing is not None
    by_name = {row.name: row for row in billing.deployments}
    assert by_name["main"].can_check_dist and not by_name["legacy"].can_check_dist
    await db.execute(delete(AllowedPath))
    await db.commit()
    outside = await _detail(db, users.superadmin, fancy)
    assert outside is not None and not any(row.can_check_dist for row in outside.deployments)
