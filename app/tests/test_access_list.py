"""The Access tab read model and its grant changes on the seeded database."""

from collections.abc import AsyncIterator, Iterable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import AccessGrant, AuditEntry, GrantLevel
from app.seeds import SeedData
from app.services.access_grid import GridUser
from app.services.access_list import (
    DISABLED_NOTICE,
    GONE_NOTICE,
    NOT_GRANTABLE_NOTICE,
    SOURCES_SHOWN,
    STALE_NOTICE,
    USER_GONE_NOTICE,
    AccessEntry,
    AccessList,
    change_access,
    load_access_list,
)
from app.services.audit import AuditActor
from app.services.grants import SUPERADMIN_REFUSAL, GrantTarget, list_grants, load_grant_tree
from app.services.projects import member_counts
from app.tests.grant_helpers import count_statements, grow

PROJECT = GrantLevel.PROJECT
DEPLOYMENT = GrantLevel.DEPLOYMENT
ENVIRONMENT = GrantLevel.ENVIRONMENT


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


def actor_of(seed_data: SeedData) -> AuditActor:
    """The superadmin as the web routes name them in audit entries."""
    return AuditActor.for_user(seed_data.users.superadmin, "10.0.0.5")


async def tab(db: AsyncSession, project_id: int, node_id: str) -> AccessList:
    """The Access tab of an item that exists."""
    found = await load_access_list(db, project_id, node_id)
    assert found is not None
    return found


def by_name(access: AccessList) -> dict[str, AccessEntry]:
    """The entries of a tab by user name."""
    return {entry.user.name: entry for entry in access.entries}


def sources_of(entry: AccessEntry) -> list[tuple[str, str]]:
    """The (name, level) of the items an entry is inherited from."""
    return [(source.name, source.level_label) for source in entry.sources]


def names(users: Iterable[GridUser]) -> set[str]:
    """The names of the users of a column list."""
    return {user.name for user in users}


async def entries_of(db: AsyncSession, action: str) -> list[AuditEntry]:
    """The audit entries of one action, oldest first."""
    rows = await db.execute(
        select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
    )
    return list(rows.scalars())


async def test_the_project_tab_lists_direct_grants_and_the_items_inside_that_carry_one(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    access = await tab(db, rentals, f"project-{rentals}")
    listed = by_name(access)
    assert set(listed) == {
        "admin",
        "viewer",
        "project_admin",
        "deployment_admin",
        "env_scoped_admin",
        "env_scoped_viewer",
    }
    assert [e.user.name for e in access.entries] == sorted(listed, key=str.lower)
    for name in ("admin", "viewer", "project_admin"):
        assert listed[name].direct and listed[name].also is None and not listed[name].sources
    assert not listed["deployment_admin"].direct
    assert sources_of(listed["deployment_admin"]) == [("prod", "Deployment")]
    assert sources_of(listed["env_scoped_admin"]) == [(".env.db", "Environment")]
    assert sources_of(listed["env_scoped_viewer"]) == [(".env.db", "Environment")]
    assert listed["deployment_admin"].below and listed["deployment_admin"].source_count == 1
    assert listed["deployment_admin"].edit_label == "Edit at source"
    assert listed["deployment_admin"].edit_href == (
        f"/deployments/{seed_data.deployments.rentals_prod.id}/access"
    )
    assert access.grantable and access.node.future_items
    assert access.subtitle == "project rentals"


async def test_superadmins_are_never_listed_and_disabled_users_are_not(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    seed_data.users.viewer.is_active = False
    await db.merge(seed_data.users.viewer)
    await db.commit()
    access = await tab(db, rentals, f"project-{rentals}")
    assert "root" not in by_name(access) and "viewer" not in by_name(access)
    assert "root" not in names(access.candidates) and "viewer" not in names(access.candidates)
    assert GrantTarget(PROJECT, rentals) in await list_grants(db, seed_data.users.viewer.id)


@pytest.mark.parametrize("project", ["rentals", "billing"])
async def test_the_listed_users_are_the_members_of_the_project_header(
    db: AsyncSession, seed_data: SeedData, project: str
) -> None:
    row = getattr(seed_data.projects, project)
    access = await tab(db, row.id, f"project-{row.id}")
    assert len(access.entries) == (await member_counts(db))[row.id]


async def test_the_deployment_tab_names_the_project_above_and_the_environments_below(
    db: AsyncSession, seed_data: SeedData
) -> None:
    prod = seed_data.deployments.rentals_prod
    access = await tab(db, seed_data.projects.rentals.id, f"deployment-{prod.id}")
    listed = by_name(access)
    assert set(listed) == set(by_name(await tab(db, prod.project_id, f"project-{prod.project_id}")))
    assert listed["deployment_admin"].direct
    for name in ("admin", "viewer", "project_admin"):
        assert sources_of(listed[name]) == [("rentals", "Project")]
        assert listed[name].edit_href == f"/projects/{prod.project_id}/access"
    assert sources_of(listed["env_scoped_admin"]) == [(".env.db", "Environment")]
    assert listed["env_scoped_admin"].below
    assert access.node.future_items and access.grantable
    assert [(p.name, p.level_label) for p in access.parents] == [("rentals", "Project")]


async def test_the_billing_deployment_lists_who_reaches_it_through_the_project_or_a_file(
    db: AsyncSession, seed_data: SeedData
) -> None:
    main = seed_data.deployments.billing_main
    listed = by_name(await tab(db, main.project_id, f"deployment-{main.id}"))
    assert set(listed) == {"admin", "env_scoped_viewer"}
    assert sources_of(listed["admin"]) == [("billing", "Project")]
    assert sources_of(listed["env_scoped_viewer"]) == [(".env.api", "Environment")]


async def test_the_service_tab_shows_the_result_and_links_the_items_that_grant_it(
    db: AsyncSession, seed_data: SeedData
) -> None:
    mysql = seed_data.services.mysql
    access = await tab(db, seed_data.projects.rentals.id, f"service-{mysql.id}")
    listed = by_name(access)
    assert not access.grantable and not access.candidates
    assert not any(entry.direct for entry in access.entries)
    assert sources_of(listed["admin"]) == [("rentals", "Project")]
    assert sources_of(listed["deployment_admin"]) == [("prod", "Deployment")]
    assert listed["admin"].sees is None and listed["deployment_admin"].sees is None
    assert sources_of(listed["env_scoped_admin"]) == [(".env.db", "Environment")]
    assert listed["env_scoped_admin"].sees == (1, 3)
    assert [(p.name, p.level_label) for p in access.parents] == [
        ("rentals", "Project"),
        ("prod", "Deployment"),
    ]
    assert access.parents[1].href == f"/deployments/{mysql.deployment_id}/access"


async def test_the_environment_tab_shows_direct_and_inherited_access_without_future_items(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    access = await tab(db, rentals, f"environment-{seed_data.envs.db.id}")
    listed = by_name(access)
    assert {name for name, entry in listed.items() if entry.direct} == {
        "env_scoped_admin",
        "env_scoped_viewer",
    }
    assert sources_of(listed["deployment_admin"]) == [("prod", "Deployment")]
    assert sources_of(listed["admin"]) == [("rentals", "Project")]
    assert not access.node.future_items
    secrets = await tab(db, rentals, f"environment-{seed_data.envs.db_secrets.id}")
    assert set(by_name(secrets)) == {"admin", "viewer", "project_admin", "deployment_admin"}


async def test_a_grant_held_again_below_a_covering_grant_is_direct_and_names_the_cover(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    environment = seed_data.envs.db
    viewer_id = seed_data.users.viewer.id
    db.add(AccessGrant(user_id=viewer_id, level=ENVIRONMENT, environment_id=environment.id))
    await db.commit()
    access = await tab(db, rentals, f"environment-{environment.id}")
    entry = by_name(access)["viewer"]
    assert entry.direct and entry.also is not None and entry.also.name == "rentals"
    assert "viewer" not in names(access.candidates)


async def test_many_items_inside_are_cut_to_a_few_and_counted(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    rentals = seed_data.projects.rentals.id
    environments = [seed_data.envs.db, seed_data.envs.db_secrets, seed_data.envs.web]
    environments.append(seed_data.editor)
    for environment in environments:
        db.add(AccessGrant(user_id=user.id, level=ENVIRONMENT, environment_id=environment.id))
    await db.commit()
    entry = by_name(await tab(db, rentals, f"project-{rentals}"))[user.name]
    assert len(entry.sources) == SOURCES_SHOWN
    assert entry.more == len(environments) - SOURCES_SHOWN
    assert entry.source_count == len(environments)
    assert (entry.edit_label, entry.edit_href) == ("Edit grants", f"/users/{user.id}")


async def test_candidates_are_the_users_a_grant_would_give_something_new(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    prod = seed_data.deployments.rentals_prod.id
    users = seed_data.users
    everyone = {
        users.admin.name,
        users.viewer.name,
        users.project_admin.name,
        users.deployment_admin.name,
        users.env_scoped_admin.name,
        users.env_scoped_viewer.name,
        users.ungranted_admin.name,
        users.totp_user.name,
        users.totp_user_alt.name,
    }
    project = await tab(db, rentals, f"project-{rentals}")
    assert names(project.candidates) == everyone - {"admin", "viewer", "project_admin"}
    deployment = await tab(db, rentals, f"deployment-{prod}")
    assert names(deployment.candidates) == everyone - {
        "admin",
        "viewer",
        "project_admin",
        "deployment_admin",
    }
    environment = await tab(db, rentals, f"environment-{seed_data.envs.db.id}")
    assert names(environment.candidates) == {
        users.ungranted_admin.name,
        users.totp_user.name,
        users.totp_user_alt.name,
    }


async def test_an_item_of_another_project_is_not_found(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals, billing = seed_data.projects.rentals.id, seed_data.projects.billing.id
    assert await load_access_list(db, billing, f"project-{rentals}") is None
    assert await load_access_list(db, rentals, "project-987654") is None
    assert await load_access_list(db, 987654, "project-987654") is None


async def test_the_project_filter_of_the_tree_reads_only_that_project(
    db: AsyncSession, seed_data: SeedData
) -> None:
    tree = await load_grant_tree(db, seed_data.projects.billing.id)
    assert [project.name for project in tree] == ["billing"]
    assert {d.name for d in tree[0].deployments} == {"main", "legacy"}
    assert await load_grant_tree(db, 987654) == ()


@pytest.mark.parametrize("kind", ["project", "deployment", "service", "environment"])
async def test_loading_takes_the_same_queries_for_a_small_and_a_large_installation(
    seeded_database: AsyncEngine, seed_data: SeedData, kind: str
) -> None:
    item_ids = {
        "project": seed_data.projects.rentals.id,
        "deployment": seed_data.deployments.rentals_prod.id,
        "service": seed_data.services.mysql.id,
        "environment": seed_data.envs.db.id,
    }
    node_id = f"{kind}-{item_ids[kind]}"
    rentals = seed_data.projects.rentals.id
    async with create_session_factory(seeded_database)() as session:
        with count_statements(seeded_database) as small:
            first = await tab(session, rentals, node_id)
        await grow(session, projects=12, users=15)
        with count_statements(seeded_database) as large:
            second = await tab(session, rentals, node_id)
    assert len(small) == len(large) == 6
    assert len(second.candidates) == len(first.candidates) + (0 if kind == "service" else 15)


async def test_adding_a_grant_writes_it_and_one_audit_entry_and_lists_the_user(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    billing = seed_data.projects.billing.id
    node_id = f"project-{billing}"
    change = await change_access(
        db, billing, node_id, user.id, grant=True, actor=actor_of(seed_data)
    )
    assert change.changed and change.notice is None and change.user_name == user.name
    assert await list_grants(db, user.id) == {GrantTarget(PROJECT, billing)}
    [entry] = await entries_of(db, "grant.add")
    assert entry.target_name == user.name
    assert entry.details == {"level": "project", "item": "billing"}
    assert entry.actor_name == "root" and entry.ip_address == "10.0.0.5"
    access = await tab(db, billing, node_id)
    assert by_name(access)[user.name].direct
    assert user.name not in names(access.candidates)


async def test_removing_a_grant_deletes_it_and_audits_it_once(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.viewer
    rentals = seed_data.projects.rentals.id
    change = await change_access(
        db, rentals, f"project-{rentals}", user.id, grant=False, actor=actor_of(seed_data)
    )
    assert change.changed and change.notice is None
    assert await list_grants(db, user.id) == set()
    [entry] = await entries_of(db, "grant.remove")
    assert entry.details == {"level": "project", "item": "rentals"}
    assert "viewer" not in by_name(await tab(db, rentals, f"project-{rentals}"))


async def test_a_deployment_and_an_environment_grant_are_added_at_their_level(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    rentals = seed_data.projects.rentals.id
    staging = seed_data.deployments.rentals_staging.id
    web = seed_data.envs.web.id
    actor = actor_of(seed_data)
    await change_access(db, rentals, f"deployment-{staging}", user.id, grant=True, actor=actor)
    await change_access(db, rentals, f"environment-{web}", user.id, grant=True, actor=actor)
    assert await list_grants(db, user.id) == {
        GrantTarget(DEPLOYMENT, staging),
        GrantTarget(ENVIRONMENT, web),
    }
    items = [entry.details["item"] for entry in await entries_of(db, "grant.add")]
    assert items == ["rentals/staging", "rentals/prod/web/.env.web"]


async def test_adding_what_the_user_already_holds_is_refused_as_an_old_page(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    change = await change_access(
        db,
        rentals,
        f"project-{rentals}",
        seed_data.users.viewer.id,
        grant=True,
        actor=actor_of(seed_data),
    )
    assert (change.changed, change.notice) == (False, STALE_NOTICE)
    assert await entries_of(db, "grant.add") == []


async def test_removing_what_the_user_no_longer_holds_is_refused_as_an_old_page(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    change = await change_access(
        db,
        rentals,
        f"project-{rentals}",
        seed_data.users.ungranted_admin.id,
        grant=False,
        actor=actor_of(seed_data),
    )
    assert (change.changed, change.notice) == (False, STALE_NOTICE)
    assert await entries_of(db, "grant.remove") == []


async def test_access_an_item_above_already_includes_cannot_be_granted_again(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    user = seed_data.users.viewer
    for node_id, covering in (
        (f"deployment-{seed_data.deployments.rentals_prod.id}", "rentals"),
        (f"environment-{seed_data.envs.web.id}", "rentals"),
    ):
        change = await change_access(
            db, rentals, node_id, user.id, grant=True, actor=actor_of(seed_data)
        )
        assert not change.changed
        assert change.notice == f"Access to this item is already included through {covering}."
    assert await list_grants(db, user.id) == {GrantTarget(PROJECT, rentals)}
    assert await entries_of(db, "grant.add") == []


async def test_a_redundant_direct_grant_can_be_removed_and_the_user_stays_listed(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    user = seed_data.users.viewer
    node_id = f"environment-{seed_data.envs.db.id}"
    db.add(AccessGrant(user_id=user.id, level=ENVIRONMENT, environment_id=seed_data.envs.db.id))
    await db.commit()
    change = await change_access(
        db, rentals, node_id, user.id, grant=False, actor=actor_of(seed_data)
    )
    assert change.changed and change.notice is None
    entry = by_name(await tab(db, rentals, node_id))["viewer"]
    assert not entry.direct and sources_of(entry) == [("rentals", "Project")]


async def test_a_superadmin_a_disabled_user_and_a_missing_user_are_refused(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals.id
    node_id = f"project-{rentals}"
    actor = actor_of(seed_data)
    root = await change_access(
        db, rentals, node_id, seed_data.users.superadmin.id, grant=True, actor=actor
    )
    assert (root.changed, root.notice) == (False, SUPERADMIN_REFUSAL)
    seed_data.users.ungranted_admin.is_active = False
    await db.merge(seed_data.users.ungranted_admin)
    await db.commit()
    for grant in (True, False):
        off = await change_access(
            db, rentals, node_id, seed_data.users.ungranted_admin.id, grant=grant, actor=actor
        )
        assert (off.changed, off.notice) == (False, DISABLED_NOTICE)
    missing = await change_access(db, rentals, node_id, 987654, grant=True, actor=actor)
    assert (missing.changed, missing.notice, missing.user_name) == (False, USER_GONE_NOTICE, None)
    assert await entries_of(db, "grant.add") == []


async def test_a_service_a_missing_item_and_an_item_of_another_project_are_refused(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals, billing = seed_data.projects.rentals.id, seed_data.projects.billing.id
    user = seed_data.users.ungranted_admin
    actor = actor_of(seed_data)
    service = await change_access(
        db, rentals, f"service-{seed_data.services.mysql.id}", user.id, grant=True, actor=actor
    )
    assert (service.changed, service.notice) == (False, NOT_GRANTABLE_NOTICE)
    for project_id, node_id in ((rentals, "project-987654"), (billing, f"project-{rentals}")):
        gone = await change_access(db, project_id, node_id, user.id, grant=True, actor=actor)
        assert (gone.changed, gone.notice) == (False, GONE_NOTICE)
    assert await list_grants(db, user.id) == set()
