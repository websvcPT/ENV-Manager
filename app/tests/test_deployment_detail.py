"""Deployment detail service: header figures, services, history and policies by scope."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import (
    AllowedPath,
    ApiToken,
    Deployment,
    DeployResult,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    DistMissingPolicy,
    DriftPolicy,
    MissingTargetPolicy,
    PostDeployStatus,
    User,
)
from app.seeds import SeedData
from app.services.deployment_detail import DeploymentDetail, load_deployment_detail
from app.services.paths import load_allowed_paths
from app.services.permissions import load_user_scope


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


async def _detail(db: AsyncSession, user: User, deployment_id: int) -> DeploymentDetail | None:
    """The deployment as a user sees it, with the allowed paths loaded fresh."""
    scope = await load_user_scope(db, user)
    return await load_deployment_detail(db, scope, deployment_id, await load_allowed_paths(db))


async def test_superadmin_sees_services_history_and_policies(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The full view carries the path, both services, newest run first and the policies."""
    detail = await _detail(db, seed_data.users.superadmin, seed_data.deployments.fancy_prod.id)
    assert detail is not None
    assert (detail.name, detail.project_name) == ("prod", "Fancy Proj. Name")
    assert detail.full and not detail.outside_allowed
    assert detail.root_path == "/opt/rentals/prod"
    assert [row.name for row in detail.services] == ["mysql", "web"]
    assert [row.environments for row in detail.services] == [3, 1]
    mysql, web = detail.services
    assert mysql.last_deploy is not None and mysql.last_deploy.status is DeployStatus.FAILED
    assert mysql.post_deploy is PostDeployStatus.SKIPPED
    assert mysql.flags == (DeployStatus.FAILED,) and web.flags == ()
    assert web.last_deploy is not None and web.last_deploy.actor == "admin"
    assert web.post_deploy is PostDeployStatus.RAN
    assert detail.flags == (DeployStatus.FAILED,)
    assert detail.can_check_dist and mysql.can_deploy and web.can_deploy
    newest, older = detail.history
    assert newest.service == "mysql" and newest.who == "root"
    assert newest.environments == ((".env.db", 2), (".env.db.secrets", 1))
    assert older.service == "web" and older.who == "admin"
    assert older.environments == ((".env.web", 1),)
    assert older.post_deploy is PostDeployStatus.RAN and older.exit_code == 0
    assert detail.last_run == newest
    assert detail.policies is not None
    assert detail.policies.dist_missing is DistMissingPolicy.BLOCK
    assert detail.policies.drift is DriftPolicy.HALT
    assert detail.policies.missing_target is MissingTargetPolicy.CREATE
    assert (detail.policies.file_mode, detail.policies.file_owner) == ("0600", "www-data")


async def test_dry_runs_are_not_history(db: AsyncSession, seed_data: SeedData) -> None:
    """A deployment that only has a dry run shows no history and no last deploy."""
    detail = await _detail(db, seed_data.users.superadmin, seed_data.deployments.billing_main.id)
    assert detail is not None
    assert detail.history == () and detail.last_run is None and detail.flags == ()
    assert [row.last_deploy for row in detail.services] == [None, None]


async def test_deployment_grant_sees_the_whole_deployment(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A deployment grant reaches both services and the policies."""
    prod = seed_data.deployments.fancy_prod.id
    detail = await _detail(db, seed_data.users.deployment_admin, prod)
    assert detail is not None and detail.full
    assert [row.name for row in detail.services] == ["mysql", "web"]
    assert detail.policies is not None and detail.root_path == "/opt/rentals/prod"


async def test_environment_grant_sees_only_its_path_to_the_file(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The parent deployment of one granted file shows no path, policies or other names."""
    prod = seed_data.deployments.fancy_prod.id
    detail = await _detail(db, seed_data.users.env_scoped_admin, prod)
    assert detail is not None and not detail.full
    assert detail.root_path is None and detail.policies is None and not detail.outside_allowed
    assert [(row.name, row.environments) for row in detail.services] == [("mysql", 1)]
    assert [row.service for row in detail.history] == ["mysql"]
    assert detail.history[0].environments == ((".env.db", 2),)
    assert detail.can_check_dist
    shown = repr(detail)
    for hidden in (".env.db.secrets", ".env.web", "web", "/opt/rentals/prod"):
        assert hidden not in shown


async def test_run_that_touched_only_hidden_files_is_left_out(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A scoped caller does not see a run of a visible service that wrote only hidden files."""
    mysql = seed_data.services.mysql
    run = (
        await db.execute(
            select(DeployRun).where(DeployRun.service_id == mysql.id).order_by(DeployRun.id)
        )
    ).scalar_one()
    await db.execute(
        delete(DeployResult).where(
            DeployResult.run_id == run.id, DeployResult.environment_id == seed_data.envs.db.id
        )
    )
    await db.commit()
    prod = seed_data.deployments.fancy_prod.id
    detail = await _detail(db, seed_data.users.env_scoped_admin, prod)
    assert detail is not None and detail.history == ()
    full = await _detail(db, seed_data.users.superadmin, seed_data.deployments.fancy_prod.id)
    assert full is not None and [row.service for row in full.history] == ["mysql", "web"]


async def test_viewer_cannot_check_or_deploy(db: AsyncSession, seed_data: SeedData) -> None:
    """A viewer reads the deployment but gets no check or deploy capability."""
    detail = await _detail(db, seed_data.users.viewer, seed_data.deployments.fancy_prod.id)
    assert detail is not None and detail.full
    assert not detail.can_check_dist
    assert not any(row.can_deploy for row in detail.services)


async def test_deployments_outside_the_scope_are_none(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Out of scope and nonexistent deployments look the same."""
    billing_main = seed_data.deployments.billing_main.id
    assert await _detail(db, seed_data.users.viewer, billing_main) is None
    assert await _detail(db, seed_data.users.ungranted_admin, billing_main) is None
    assert await _detail(db, seed_data.users.superadmin, 9999) is None


async def test_environment_grant_in_another_project_reaches_only_its_parent(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The parent of a granted billing file is shown by name, with that file's service only."""
    detail = await _detail(
        db, seed_data.users.env_scoped_viewer, seed_data.deployments.billing_main.id
    )
    assert detail is not None and not detail.full
    assert [row.name for row in detail.services] == ["api"]
    assert not detail.can_check_dist


async def test_a_deployment_outside_the_allowed_paths_is_flagged(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The legacy deployment is outside the list, and removing an entry flags what lay under it."""
    legacy = (await db.execute(select(Deployment).where(Deployment.name == "legacy"))).scalar_one()
    detail = await _detail(db, seed_data.users.superadmin, legacy.id)
    assert detail is not None and detail.outside_allowed
    assert [row.name for row in detail.services] == ["cron"]
    prod = seed_data.deployments.fancy_prod.id
    before = await _detail(db, seed_data.users.superadmin, prod)
    assert before is not None and not before.outside_allowed
    await db.execute(delete(AllowedPath).where(AllowedPath.path == "/opt/rentals"))
    await db.commit()
    after = await _detail(db, seed_data.users.superadmin, prod)
    assert after is not None and after.outside_allowed


async def test_token_runs_name_the_token_and_its_owner(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A run started by an API token is attributed to the token and the user it acts for."""
    token = (await db.execute(select(ApiToken).where(ApiToken.name == "billing-ci"))).scalar_one()
    db.add(
        DeployRun(
            service_id=seed_data.services.api.id,
            service_name="api",
            deployment_name="main",
            triggered_by_token_id=token.id,
            trigger=DeployTrigger.API,
            status=DeployStatus.SUCCESS,
        )
    )
    await db.commit()
    detail = await _detail(db, seed_data.users.superadmin, seed_data.deployments.billing_main.id)
    assert detail is not None
    assert [row.who for row in detail.history] == ["token billing-ci (admin)"]
    assert detail.last_run is not None and detail.last_run.status is DeployStatus.SUCCESS
