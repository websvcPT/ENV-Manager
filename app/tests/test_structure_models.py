"""Tests for the structure, version, grant and token restriction tables."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import (
    AccessGrant,
    ApiToken,
    ApiTokenRestriction,
    Deployment,
    DistMissingPolicy,
    DriftPolicy,
    Environment,
    EnvironmentVersion,
    GrantLevel,
    HookType,
    MissingTargetPolicy,
    Project,
    RestrictionLevel,
    Service,
    ServiceHook,
    User,
    UserGroup,
)

PLACEHOLDER_HASH = "not-a-real-hash"


class Tree:
    """Ids of one project with a deployment, a service, two environments and a user."""

    def __init__(self) -> None:
        self.user_id = 0
        self.project_id = 0
        self.deployment_id = 0
        self.service_id = 0
        self.environment_ids: list[int] = []


async def _build_tree(session: AsyncSession) -> Tree:
    """Persist a minimal project tree and return its ids."""
    tree = Tree()
    user = User(
        name="alice",
        email="a@example.test",
        password_hash=PLACEHOLDER_HASH,
        group=UserGroup.ADMIN,
    )
    project = Project(name="acme-shop")
    session.add_all([user, project])
    await session.flush()
    deployment = Deployment(project_id=project.id, name="deployment-a", root_path="/opt/a")
    session.add(deployment)
    await session.flush()
    service = Service(deployment_id=deployment.id, name="mysql")
    session.add(service)
    await session.flush()
    environments = [
        Environment(service_id=service.id, name=name, target_path=f"environments/{name}")
        for name in (".env.db", ".env.db.secrets")
    ]
    session.add_all(environments)
    await session.flush()
    tree.user_id = user.id
    tree.project_id = project.id
    tree.deployment_id = deployment.id
    tree.service_id = service.id
    tree.environment_ids = [environment.id for environment in environments]
    await session.commit()
    return tree


async def test_policy_defaults_and_overrides(engine: AsyncEngine) -> None:
    """Deployments default to the safe policies and environments inherit unless overridden."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        environment = await session.get(Environment, tree.environment_ids[0])
        assert environment is not None
        environment.drift_policy = DriftPolicy.BACKUP_OVERWRITE
        await session.commit()
    async with factory() as session:
        deployment = await session.get(Deployment, tree.deployment_id)
        environments = (
            (await session.execute(select(Environment).order_by(Environment.id))).scalars().all()
        )
        raw = (await session.execute(text("SELECT drift_policy FROM environments"))).scalars()
    assert deployment is not None
    assert deployment.dist_missing_policy is DistMissingPolicy.BLOCK
    assert deployment.drift_policy is DriftPolicy.HALT
    assert deployment.missing_target_policy is MissingTargetPolicy.CREATE
    assert environments[0].drift_policy is DriftPolicy.BACKUP_OVERWRITE
    assert environments[1].drift_policy is None
    assert sorted(value for value in raw if value) == ["backup_overwrite"]


async def test_names_are_unique_within_their_parent(engine: AsyncEngine) -> None:
    """Names collide only under the same parent."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        session.add(Project(name="acme-shop"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        session.add(Service(deployment_id=tree.deployment_id, name="mysql"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        session.add(Environment(service_id=tree.service_id, name=".env.db", target_path="x"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        other = Project(name="other")
        session.add(other)
        await session.flush()
        session.add(Deployment(project_id=other.id, name="deployment-a", root_path="/opt/b"))
        await session.commit()


async def test_deleting_a_project_removes_everything_below(engine: AsyncEngine) -> None:
    """Deleting a project cascades to deployments, services, environments, versions, grants."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        session.add(
            EnvironmentVersion(
                environment_id=tree.environment_ids[0],
                number=1,
                content_encrypted="ciphertext",
                content_hash="a" * 64,
            )
        )
        session.add(ServiceHook(service_id=tree.service_id, type=HookType.POST_DEPLOY))
        session.add(
            AccessGrant(user_id=tree.user_id, level=GrantLevel.PROJECT, project_id=tree.project_id)
        )
        session.add(
            AccessGrant(
                user_id=tree.user_id,
                level=GrantLevel.ENVIRONMENT,
                environment_id=tree.environment_ids[1],
            )
        )
        await session.commit()
    async with factory() as session:
        await session.delete(await session.get(Project, tree.project_id))
        await session.commit()
        for model in (Deployment, Service, Environment, EnvironmentVersion, ServiceHook):
            assert (await session.execute(select(model))).scalars().all() == []
        assert (await session.execute(select(AccessGrant))).scalars().all() == []
        assert await session.get(User, tree.user_id) is not None


async def test_version_numbers_unique_per_environment_and_author_optional(
    engine: AsyncEngine,
) -> None:
    """A version number repeats across environments only, and a deleted author leaves it."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        for environment_id in tree.environment_ids:
            session.add(
                EnvironmentVersion(
                    environment_id=environment_id,
                    number=1,
                    content_encrypted="c",
                    content_hash="b" * 64,
                    comment="first",
                    author_id=tree.user_id,
                )
            )
        await session.commit()
    async with factory() as session:
        session.add(
            EnvironmentVersion(
                environment_id=tree.environment_ids[0],
                number=1,
                content_encrypted="c",
                content_hash="b" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        await session.delete(await session.get(User, tree.user_id))
        await session.commit()
        versions = (await session.execute(select(EnvironmentVersion))).scalars().all()
    assert len(versions) == 2
    assert all(version.author_id is None for version in versions)


async def test_hook_defaults_and_one_hook_per_type(engine: AsyncEngine) -> None:
    """Hooks default to a 120 second timeout and run-if-changed, one per type per service."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        session.add(ServiceHook(service_id=tree.service_id, type=HookType.POST_DEPLOY))
        await session.commit()
    async with factory() as session:
        hook = (await session.execute(select(ServiceHook))).scalar_one()
        raw_type = (await session.execute(text("SELECT type FROM service_hooks"))).scalar_one()
        assert hook.timeout_seconds == 120
        assert hook.run_only_if_changed is True
        assert hook.working_directory is None
        assert raw_type == "post_deploy"
        session.add(ServiceHook(service_id=tree.service_id, type=HookType.POST_DEPLOY))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        session.add(ServiceHook(service_id=tree.service_id, type=HookType.PRE_DEPLOY))
        await session.commit()


async def test_grant_target_must_match_level(engine: AsyncEngine) -> None:
    """A grant names exactly the target of its level."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
    invalid = [
        AccessGrant(user_id=tree.user_id, level=GrantLevel.PROJECT),
        AccessGrant(
            user_id=tree.user_id, level=GrantLevel.PROJECT, deployment_id=tree.deployment_id
        ),
        AccessGrant(
            user_id=tree.user_id,
            level=GrantLevel.DEPLOYMENT,
            project_id=tree.project_id,
            deployment_id=tree.deployment_id,
        ),
    ]
    for grant in invalid:
        async with factory() as session:
            session.add(grant)
            with pytest.raises(IntegrityError):
                await session.commit()


async def test_grants_are_unique_per_user_and_target(engine: AsyncEngine) -> None:
    """The same user cannot hold the same grant twice."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        session.add(
            AccessGrant(
                user_id=tree.user_id,
                level=GrantLevel.DEPLOYMENT,
                deployment_id=tree.deployment_id,
            )
        )
        await session.commit()
    async with factory() as session:
        session.add(
            AccessGrant(
                user_id=tree.user_id,
                level=GrantLevel.DEPLOYMENT,
                deployment_id=tree.deployment_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_revoking_a_user_removes_grants(engine: AsyncEngine) -> None:
    """Deleting a user removes their grants and nothing else."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        session.add(
            AccessGrant(user_id=tree.user_id, level=GrantLevel.PROJECT, project_id=tree.project_id)
        )
        await session.commit()
    async with factory() as session:
        await session.delete(await session.get(User, tree.user_id))
        await session.commit()
        assert (await session.execute(select(AccessGrant))).scalars().all() == []
        assert await session.get(Project, tree.project_id) is not None


async def test_token_restrictions_hold_a_set_of_environments(engine: AsyncEngine) -> None:
    """A token can be narrowed to several environments, each once."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        token = ApiToken(owner_id=tree.user_id, name="ci", token_hash="d" * 64, prefix="emt_abcd")
        session.add(token)
        await session.flush()
        token_id = token.id
        for environment_id in tree.environment_ids:
            session.add(
                ApiTokenRestriction(
                    token_id=token_id,
                    level=RestrictionLevel.ENVIRONMENT,
                    environment_id=environment_id,
                )
            )
        await session.commit()
    async with factory() as session:
        rows = (await session.execute(select(ApiTokenRestriction))).scalars().all()
        assert {row.environment_id for row in rows} == set(tree.environment_ids)
        session.add(
            ApiTokenRestriction(
                token_id=token_id,
                level=RestrictionLevel.ENVIRONMENT,
                environment_id=tree.environment_ids[0],
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_token_restriction_target_must_match_level(engine: AsyncEngine) -> None:
    """A restriction names exactly the target of its level."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        token = ApiToken(owner_id=tree.user_id, name="ci", token_hash="e" * 64, prefix="emt_abcd")
        session.add(token)
        await session.commit()
        token_id = token.id
    invalid = [
        ApiTokenRestriction(token_id=token_id, level=RestrictionLevel.SERVICE),
        ApiTokenRestriction(
            token_id=token_id,
            level=RestrictionLevel.SERVICE,
            service_id=tree.service_id,
            project_id=tree.project_id,
        ),
        ApiTokenRestriction(
            token_id=token_id, level=RestrictionLevel.PROJECT, service_id=tree.service_id
        ),
    ]
    for restriction in invalid:
        async with factory() as session:
            session.add(restriction)
            with pytest.raises(IntegrityError):
                await session.commit()
    async with factory() as session:
        session.add(
            ApiTokenRestriction(
                token_id=token_id, level=RestrictionLevel.SERVICE, service_id=tree.service_id
            )
        )
        await session.commit()


async def test_restricted_target_cannot_be_deleted_while_a_token_points_at_it(
    engine: AsyncEngine,
) -> None:
    """Deleting a restricted service is refused, so the token is never silently widened."""
    factory = create_session_factory(engine)
    async with factory() as session:
        tree = await _build_tree(session)
        token = ApiToken(owner_id=tree.user_id, name="ci", token_hash="f" * 64, prefix="emt_abcd")
        session.add(token)
        await session.flush()
        session.add(
            ApiTokenRestriction(
                token_id=token.id, level=RestrictionLevel.SERVICE, service_id=tree.service_id
            )
        )
        await session.commit()
        token_id = token.id
    async with factory() as session:
        await session.delete(await session.get(Service, tree.service_id))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        await session.delete(await session.get(ApiToken, token_id))
        await session.commit()
        assert (await session.execute(select(ApiTokenRestriction))).scalars().all() == []
        await session.delete(await session.get(Service, tree.service_id))
        await session.commit()
