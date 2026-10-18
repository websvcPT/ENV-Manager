"""Grants: the tree to choose from, parsing chosen items and audited changes to a user's set."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import AccessGrant, AuditEntry, AuditResult, GrantLevel, Project
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.grants import (
    GrantError,
    GrantTarget,
    StaleGrantsError,
    describe_targets,
    list_grants,
    load_grant_tree,
    parse_targets,
    projects_reached,
    set_user_grants,
)
from app.services.permissions import ItemLevel, load_item, load_user_scope


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


async def count_entries(db: AsyncSession) -> int:
    """How many audit entries exist."""
    return len((await db.execute(select(AuditEntry))).scalars().all())


def actor_of(seed_data: SeedData) -> AuditActor:
    """The superadmin as the web routes name them in audit entries."""
    return AuditActor.for_user(seed_data.users.superadmin, "10.0.0.5")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("project:3", GrantTarget(GrantLevel.PROJECT, 3)),
        (" environment:12 ", GrantTarget(GrantLevel.ENVIRONMENT, 12)),
        ("deployment:7", GrantTarget(GrantLevel.DEPLOYMENT, 7)),
        ("service:3", None),
        ("project:0", None),
        ("project:-1", None),
        ("project:1.5", None),
        ("project:٣", None),
        ("project:", None),
        ("project", None),
        ("", None),
        ("project:1234567890123456", None),
    ],
)
def test_a_target_parses_only_from_level_and_a_positive_whole_number(
    value: str, expected: GrantTarget | None
) -> None:
    assert GrantTarget.parse(value) == expected
    if expected is not None:
        assert GrantTarget.parse(expected.key) == expected


def test_one_bad_value_refuses_the_whole_choice() -> None:
    assert parse_targets(["project:1", "project:1", "environment:2"]) == {
        GrantTarget(GrantLevel.PROJECT, 1),
        GrantTarget(GrantLevel.ENVIRONMENT, 2),
    }
    with pytest.raises(GrantError):
        parse_targets(["project:1", "nonsense"])


async def test_the_tree_lists_everything_by_name_with_services_as_context(
    db: AsyncSession, seed_data: SeedData
) -> None:
    tree = await load_grant_tree(db)
    assert [project.name for project in tree] == ["billing", "rentals"]
    rentals = tree[1]
    assert [deployment.name for deployment in rentals.deployments] == ["prod", "staging"]
    prod = rentals.deployments[0]
    assert [service.name for service in prod.services] == ["mysql", "web"]
    mysql = prod.services[0]
    assert [environment.name for environment in mysql.environments] == [
        ".env.db",
        ".env.db.app",
        ".env.db.secrets",
    ]
    assert mysql.environments[0].id == seed_data.envs.db.id
    legacy = next(d for d in tree[0].deployments if d.name == "legacy")
    assert [service.name for service in legacy.services] == ["cron"]
    assert legacy.services[0].environments == ()


async def test_a_path_is_given_for_items_that_exist_and_left_out_for_others(
    db: AsyncSession, seed_data: SeedData
) -> None:
    present = [
        GrantTarget(GrantLevel.PROJECT, seed_data.projects.rentals.id),
        GrantTarget(GrantLevel.DEPLOYMENT, seed_data.deployments.rentals_prod.id),
        GrantTarget(GrantLevel.ENVIRONMENT, seed_data.envs.db.id),
    ]
    missing = GrantTarget(GrantLevel.ENVIRONMENT, 424242)
    labels = await describe_targets(db, [*present, missing])
    assert [labels[target] for target in present] == [
        "rentals",
        "rentals/prod",
        "rentals/prod/mysql/.env.db",
    ]
    assert missing not in labels


async def test_saving_adds_and_removes_grants_audits_each_and_applies_at_once(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    rentals = GrantTarget(GrantLevel.PROJECT, seed_data.projects.rentals.id)
    secrets = GrantTarget(GrantLevel.ENVIRONMENT, seed_data.envs.db_secrets.id)
    item = await load_item(db, ItemLevel.ENVIRONMENT, seed_data.envs.db_secrets.id)
    assert item is not None
    assert not (await load_user_scope(db, user)).covers(item)

    change = await set_user_grants(
        db, user, {rentals, secrets}, actor=actor_of(seed_data), base=frozenset()
    )
    assert set(change.added) == {rentals, secrets} and change.removed == ()
    assert await list_grants(db, user.id) == frozenset({rentals, secrets})
    assert (await load_user_scope(db, user)).covers(item)

    change = await set_user_grants(db, user, {secrets}, actor=actor_of(seed_data))
    assert change.added == () and change.removed == (rentals,)
    assert await list_grants(db, user.id) == frozenset({secrets})

    entries = (
        (await db.execute(select(AuditEntry).where(AuditEntry.action.like("grant.%"))))
        .scalars()
        .all()
    )
    assert sorted((entry.action, str(entry.details["item"])) for entry in entries) == [
        ("grant.add", "rentals"),
        ("grant.add", "rentals/prod/mysql/.env.db.secrets"),
        ("grant.remove", "rentals"),
    ]
    for entry in entries:
        assert entry.result is AuditResult.SUCCESS
        assert (entry.actor_name, entry.ip_address) == ("root", "10.0.0.5")
        assert (entry.target_type, entry.target_name) == ("user", user.name)
        assert (entry.project_id, entry.deployment_id, entry.service_id) == (None, None, None)
        assert entry.environment_id is None


async def test_removing_the_last_grant_takes_the_access_away_at_once(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.env_scoped_admin
    item = await load_item(db, ItemLevel.ENVIRONMENT, seed_data.envs.db.id)
    assert item is not None
    assert (await load_user_scope(db, user)).covers(item)
    await set_user_grants(db, user, set(), actor=actor_of(seed_data))
    assert not (await load_user_scope(db, user)).covers(item)
    assert await list_grants(db, user.id) == frozenset()


async def test_saving_what_is_already_stored_changes_and_audits_nothing(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.project_admin
    stored = await list_grants(db, user.id)
    before = await count_entries(db)
    change = await set_user_grants(db, user, set(stored), actor=actor_of(seed_data), base=stored)
    assert not change.changed
    assert await count_entries(db) == before


async def test_a_page_older_than_the_stored_grants_is_refused_and_nothing_changes(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.project_admin
    stored = await list_grants(db, user.id)
    billing = GrantTarget(GrantLevel.PROJECT, seed_data.projects.billing.id)
    before = await count_entries(db)
    with pytest.raises(StaleGrantsError):
        await set_user_grants(
            db, user, {billing}, actor=actor_of(seed_data), base=frozenset({billing})
        )
    assert await list_grants(db, user.id) == stored
    assert await count_entries(db) == before


async def test_a_deleted_item_is_refused_before_anything_is_written(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    rentals = GrantTarget(GrantLevel.PROJECT, seed_data.projects.rentals.id)
    ghost = GrantTarget(GrantLevel.ENVIRONMENT, 424242)
    before = await count_entries(db)
    with pytest.raises(GrantError) as refused:
        await set_user_grants(db, user, {rentals, ghost}, actor=actor_of(seed_data))
    assert refused.value.field == "grants" and "no longer exists" in str(refused.value)
    assert await list_grants(db, user.id) == frozenset()
    assert await count_entries(db) == before


async def test_a_superadmin_holds_no_grants(db: AsyncSession, seed_data: SeedData) -> None:
    rentals = GrantTarget(GrantLevel.PROJECT, seed_data.projects.rentals.id)
    with pytest.raises(GrantError):
        await set_user_grants(db, seed_data.users.superadmin, {rentals}, actor=actor_of(seed_data))
    assert await list_grants(db, seed_data.users.superadmin.id) == frozenset()


async def test_projects_reached_counts_a_project_once_whatever_the_grant_level(
    db: AsyncSession, seed_data: SeedData
) -> None:
    reached = await projects_reached(db)
    users = seed_data.users
    rentals, billing = seed_data.projects.rentals.id, seed_data.projects.billing.id
    assert reached[users.admin.id] == {rentals, billing}
    assert reached[users.project_admin.id] == {rentals}
    assert reached[users.deployment_admin.id] == {rentals}
    assert reached[users.env_scoped_viewer.id] == {rentals, billing}
    assert users.ungranted_admin.id not in reached
    assert users.superadmin.id not in reached


async def test_a_deleted_project_takes_its_grants_with_it(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    rentals = seed_data.projects.rentals.id
    db.add(AccessGrant(user_id=user.id, level=GrantLevel.PROJECT, project_id=rentals))
    await db.commit()
    assert await list_grants(db, user.id) == frozenset({GrantTarget(GrantLevel.PROJECT, rentals)})
    project = await db.get(Project, rentals)
    assert project is not None
    await db.delete(project)
    await db.commit()
    assert await list_grants(db, user.id) == frozenset()
