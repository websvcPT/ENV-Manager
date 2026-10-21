"""The Access tab read model and its grant changes on the seeded database."""

from collections.abc import Iterable

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import AccessGrant
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
    load_removal,
    remove_access,
)
from app.services.grants import SUPERADMIN_REFUSAL, GrantTarget, list_grants, load_grant_tree
from app.services.projects import member_counts
from app.tests.audit_helpers import audit_entries
from app.tests.grant_helpers import (
    DEPLOYMENT,
    ENVIRONMENT,
    PROJECT,
    actor_of,
    count_statements,
    grow,
)


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


async def test_the_project_tab_lists_direct_grants_and_the_items_inside_that_carry_one(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    access = await tab(db, fancy, f"project-{fancy}")
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
    assert sources_of(listed["env_scoped_admin"]) == [("mysql/.env.db", "Environment")]
    assert sources_of(listed["env_scoped_viewer"]) == [("mysql/.env.db", "Environment")]
    assert listed["deployment_admin"].below and listed["deployment_admin"].source_count == 1
    assert listed["deployment_admin"].remove_label == "Remove the grant on deployment prod"
    assert listed["admin"].remove_label == "Remove access"
    assert access.grantable and access.node.future_items
    assert access.subtitle == "project Fancy Proj. Name"


async def test_superadmins_are_never_listed_and_disabled_users_are_not(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    seed_data.users.viewer.is_active = False
    await db.merge(seed_data.users.viewer)
    await db.commit()
    access = await tab(db, fancy, f"project-{fancy}")
    assert "root" not in by_name(access) and "viewer" not in by_name(access)
    assert "root" not in names(access.candidates) and "viewer" not in names(access.candidates)
    assert GrantTarget(PROJECT, fancy) in await list_grants(db, seed_data.users.viewer.id)


@pytest.mark.parametrize("project", ["fancy", "billing"])
async def test_the_listed_users_are_the_members_of_the_project_header(
    db: AsyncSession, seed_data: SeedData, project: str
) -> None:
    row = getattr(seed_data.projects, project)
    access = await tab(db, row.id, f"project-{row.id}")
    assert len(access.entries) == (await member_counts(db))[row.id]


async def test_the_deployment_tab_names_the_project_above_and_the_environments_below(
    db: AsyncSession, seed_data: SeedData
) -> None:
    prod = seed_data.deployments.fancy_prod
    access = await tab(db, seed_data.projects.fancy.id, f"deployment-{prod.id}")
    listed = by_name(access)
    assert set(listed) == set(by_name(await tab(db, prod.project_id, f"project-{prod.project_id}")))
    assert listed["deployment_admin"].direct
    for name in ("admin", "viewer", "project_admin"):
        assert sources_of(listed[name]) == [("Fancy Proj. Name", "Project")]
        assert listed[name].remove_label == "Remove the grant on project Fancy Proj. Name"
    assert sources_of(listed["env_scoped_admin"]) == [("mysql/.env.db", "Environment")]
    assert listed["env_scoped_admin"].below
    assert access.node.future_items and access.grantable
    assert [(p.name, p.level_label) for p in access.parents] == [("Fancy Proj. Name", "Project")]


async def test_the_billing_deployment_lists_who_reaches_it_through_the_project_or_a_file(
    db: AsyncSession, seed_data: SeedData
) -> None:
    main = seed_data.deployments.billing_main
    listed = by_name(await tab(db, main.project_id, f"deployment-{main.id}"))
    assert set(listed) == {"admin", "env_scoped_viewer"}
    assert sources_of(listed["admin"]) == [("billing", "Project")]
    assert sources_of(listed["env_scoped_viewer"]) == [("api/.env.api", "Environment")]


async def test_the_service_tab_shows_the_result_and_links_the_items_that_grant_it(
    db: AsyncSession, seed_data: SeedData
) -> None:
    mysql = seed_data.services.mysql
    access = await tab(db, seed_data.projects.fancy.id, f"service-{mysql.id}")
    listed = by_name(access)
    assert not access.grantable and not access.candidates
    assert not any(entry.direct for entry in access.entries)
    assert sources_of(listed["admin"]) == [("Fancy Proj. Name", "Project")]
    assert sources_of(listed["deployment_admin"]) == [("prod", "Deployment")]
    assert listed["admin"].sees is None and listed["deployment_admin"].sees is None
    assert sources_of(listed["env_scoped_admin"]) == [("mysql/.env.db", "Environment")]
    assert listed["env_scoped_admin"].sees == (1, 3)
    assert [(p.name, p.level_label) for p in access.parents] == [
        ("Fancy Proj. Name", "Project"),
        ("prod", "Deployment"),
    ]
    assert access.parents[1].href == f"/deployments/{mysql.deployment_id}/access"


async def test_the_environment_tab_shows_direct_and_inherited_access_without_future_items(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    access = await tab(db, fancy, f"environment-{seed_data.envs.db.id}")
    assert access.subtitle == "environment mysql/.env.db"
    listed = by_name(access)
    assert {name for name, entry in listed.items() if entry.direct} == {
        "env_scoped_admin",
        "env_scoped_viewer",
    }
    assert sources_of(listed["deployment_admin"]) == [("prod", "Deployment")]
    assert sources_of(listed["admin"]) == [("Fancy Proj. Name", "Project")]
    assert not access.node.future_items
    secrets = await tab(db, fancy, f"environment-{seed_data.envs.db_secrets.id}")
    assert set(by_name(secrets)) == {"admin", "viewer", "project_admin", "deployment_admin"}


async def test_a_grant_held_again_below_a_covering_grant_is_direct_and_names_the_cover(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    environment = seed_data.envs.db
    viewer_id = seed_data.users.viewer.id
    db.add(AccessGrant(user_id=viewer_id, level=ENVIRONMENT, environment_id=environment.id))
    await db.commit()
    access = await tab(db, fancy, f"environment-{environment.id}")
    entry = by_name(access)["viewer"]
    assert entry.direct and entry.also is not None and entry.also.name == "Fancy Proj. Name"
    assert "viewer" not in names(access.candidates)


async def test_many_items_inside_are_cut_to_a_few_and_counted(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    fancy = seed_data.projects.fancy.id
    environments = [seed_data.envs.db, seed_data.envs.db_secrets, seed_data.envs.web]
    environments.append(seed_data.editor)
    for environment in environments:
        db.add(AccessGrant(user_id=user.id, level=ENVIRONMENT, environment_id=environment.id))
    await db.commit()
    entry = by_name(await tab(db, fancy, f"project-{fancy}"))[user.name]
    assert len(entry.sources) == SOURCES_SHOWN
    assert entry.more == len(environments) - SOURCES_SHOWN
    assert entry.source_count == len(environments)
    assert entry.remove_label == "Remove the grants that give this access"


async def test_candidates_are_the_users_a_grant_would_give_something_new(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    prod = seed_data.deployments.fancy_prod.id
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
    project = await tab(db, fancy, f"project-{fancy}")
    assert names(project.candidates) == everyone - {"admin", "viewer", "project_admin"}
    deployment = await tab(db, fancy, f"deployment-{prod}")
    assert names(deployment.candidates) == everyone - {
        "admin",
        "viewer",
        "project_admin",
        "deployment_admin",
    }
    environment = await tab(db, fancy, f"environment-{seed_data.envs.db.id}")
    assert names(environment.candidates) == {
        users.ungranted_admin.name,
        users.totp_user.name,
        users.totp_user_alt.name,
    }


async def test_an_item_of_another_project_is_not_found(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy, billing = seed_data.projects.fancy.id, seed_data.projects.billing.id
    assert await load_access_list(db, billing, f"project-{fancy}") is None
    assert await load_access_list(db, fancy, "project-987654") is None
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
        "project": seed_data.projects.fancy.id,
        "deployment": seed_data.deployments.fancy_prod.id,
        "service": seed_data.services.mysql.id,
        "environment": seed_data.envs.db.id,
    }
    node_id = f"{kind}-{item_ids[kind]}"
    fancy = seed_data.projects.fancy.id
    async with create_session_factory(seeded_database)() as session:
        with count_statements(seeded_database) as small:
            first = await tab(session, fancy, node_id)
        await grow(session, projects=12, users=15)
        with count_statements(seeded_database) as large:
            second = await tab(session, fancy, node_id)
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
    [entry] = await audit_entries(db, "grant.add")
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
    fancy = seed_data.projects.fancy.id
    change = await change_access(
        db, fancy, f"project-{fancy}", user.id, grant=False, actor=actor_of(seed_data)
    )
    assert change.changed and change.notice is None
    assert await list_grants(db, user.id) == set()
    [entry] = await audit_entries(db, "grant.remove")
    assert entry.details == {"level": "project", "item": "Fancy Proj. Name"}
    assert "viewer" not in by_name(await tab(db, fancy, f"project-{fancy}"))


async def test_a_deployment_and_an_environment_grant_are_added_at_their_level(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    fancy = seed_data.projects.fancy.id
    staging = seed_data.deployments.fancy_staging.id
    web = seed_data.envs.web.id
    actor = actor_of(seed_data)
    await change_access(db, fancy, f"deployment-{staging}", user.id, grant=True, actor=actor)
    await change_access(db, fancy, f"environment-{web}", user.id, grant=True, actor=actor)
    assert await list_grants(db, user.id) == {
        GrantTarget(DEPLOYMENT, staging),
        GrantTarget(ENVIRONMENT, web),
    }
    items = [entry.details["item"] for entry in await audit_entries(db, "grant.add")]
    assert items == ["Fancy Proj. Name/staging", "Fancy Proj. Name/prod/web/.env.web"]


async def test_adding_what_the_user_already_holds_is_refused_as_an_old_page(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    change = await change_access(
        db,
        fancy,
        f"project-{fancy}",
        seed_data.users.viewer.id,
        grant=True,
        actor=actor_of(seed_data),
    )
    assert (change.changed, change.notice) == (False, STALE_NOTICE)
    assert await audit_entries(db, "grant.add") == []


async def test_removing_what_the_user_no_longer_holds_is_refused_as_an_old_page(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    change = await change_access(
        db,
        fancy,
        f"project-{fancy}",
        seed_data.users.ungranted_admin.id,
        grant=False,
        actor=actor_of(seed_data),
    )
    assert (change.changed, change.notice) == (False, STALE_NOTICE)
    assert await audit_entries(db, "grant.remove") == []


async def test_access_an_item_above_already_includes_cannot_be_granted_again(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    user = seed_data.users.viewer
    for node_id, covering in (
        (f"deployment-{seed_data.deployments.fancy_prod.id}", "Fancy Proj. Name"),
        (f"environment-{seed_data.envs.web.id}", "Fancy Proj. Name"),
    ):
        change = await change_access(
            db, fancy, node_id, user.id, grant=True, actor=actor_of(seed_data)
        )
        assert not change.changed
        assert change.notice == f"Access to this item is already included through {covering}."
    assert await list_grants(db, user.id) == {GrantTarget(PROJECT, fancy)}
    assert await audit_entries(db, "grant.add") == []


async def test_a_redundant_direct_grant_can_be_removed_and_the_user_stays_listed(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    user = seed_data.users.viewer
    node_id = f"environment-{seed_data.envs.db.id}"
    db.add(AccessGrant(user_id=user.id, level=ENVIRONMENT, environment_id=seed_data.envs.db.id))
    await db.commit()
    change = await change_access(
        db, fancy, node_id, user.id, grant=False, actor=actor_of(seed_data)
    )
    assert change.changed and change.notice is None
    entry = by_name(await tab(db, fancy, node_id))["viewer"]
    assert not entry.direct and sources_of(entry) == [("Fancy Proj. Name", "Project")]


async def test_a_superadmin_a_disabled_user_and_a_missing_user_are_refused(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy = seed_data.projects.fancy.id
    node_id = f"project-{fancy}"
    actor = actor_of(seed_data)
    root = await change_access(
        db, fancy, node_id, seed_data.users.superadmin.id, grant=True, actor=actor
    )
    assert (root.changed, root.notice) == (False, SUPERADMIN_REFUSAL)
    seed_data.users.ungranted_admin.is_active = False
    await db.merge(seed_data.users.ungranted_admin)
    await db.commit()
    for grant in (True, False):
        off = await change_access(
            db, fancy, node_id, seed_data.users.ungranted_admin.id, grant=grant, actor=actor
        )
        assert (off.changed, off.notice) == (False, DISABLED_NOTICE)
    missing = await change_access(db, fancy, node_id, 987654, grant=True, actor=actor)
    assert (missing.changed, missing.notice, missing.user_name) == (False, USER_GONE_NOTICE, None)
    assert await audit_entries(db, "grant.add") == []


async def test_a_service_a_missing_item_and_an_item_of_another_project_are_refused(
    db: AsyncSession, seed_data: SeedData
) -> None:
    fancy, billing = seed_data.projects.fancy.id, seed_data.projects.billing.id
    user = seed_data.users.ungranted_admin
    actor = actor_of(seed_data)
    service = await change_access(
        db, fancy, f"service-{seed_data.services.mysql.id}", user.id, grant=True, actor=actor
    )
    assert (service.changed, service.notice) == (False, NOT_GRANTABLE_NOTICE)
    for project_id, node_id in ((fancy, "project-987654"), (billing, f"project-{fancy}")):
        gone = await change_access(db, project_id, node_id, user.id, grant=True, actor=actor)
        assert (gone.changed, gone.notice) == (False, GONE_NOTICE)
    assert await list_grants(db, user.id) == set()


async def test_removing_an_inherited_access_takes_away_the_grant_above_that_gives_it(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.deployment_admin
    fancy = seed_data.projects.fancy.id
    node_id = f"environment-{seed_data.envs.db.id}"
    entry = by_name(await tab(db, fancy, node_id))[user.name]
    assert not entry.direct and not entry.below
    assert entry.remove_label == "Remove the grant on deployment prod"
    sources = await load_removal(db, fancy, node_id, user.id)
    assert sources is not None
    assert [(source.name, source.level_label) for source in sources] == [("prod", "Deployment")]
    change = await remove_access(db, fancy, node_id, user.id, actor=actor_of(seed_data))
    assert change.changed and change.notice is None and change.user_name == user.name
    assert await list_grants(db, user.id) == set()
    [audited] = await audit_entries(db, "grant.remove")
    assert audited.details == {"level": "deployment", "item": "Fancy Proj. Name/prod"}
    assert user.name not in by_name(await tab(db, fancy, node_id))


async def test_removing_access_through_items_inside_takes_away_those_grants(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.env_scoped_admin
    fancy = seed_data.projects.fancy.id
    node_id = f"service-{seed_data.services.mysql.id}"
    entry = by_name(await tab(db, fancy, node_id))[user.name]
    assert entry.below
    sources = await load_removal(db, fancy, node_id, user.id)
    assert sources is not None
    assert [source.name for source in sources] == ["mysql/.env.db"]
    change = await remove_access(db, fancy, node_id, user.id, actor=actor_of(seed_data))
    assert change.changed and change.notice is None
    assert await list_grants(db, user.id) == set()
    assert user.name not in by_name(await tab(db, fancy, node_id))


async def test_removing_a_direct_access_takes_away_only_that_grant(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.viewer
    fancy = seed_data.projects.fancy.id
    change = await remove_access(db, fancy, f"project-{fancy}", user.id, actor=actor_of(seed_data))
    assert change.changed and change.notice is None
    assert await list_grants(db, user.id) == set()


async def test_removing_access_a_user_does_not_have_is_refused_as_an_old_page(
    db: AsyncSession, seed_data: SeedData
) -> None:
    user = seed_data.users.ungranted_admin
    fancy = seed_data.projects.fancy.id
    node_id = f"environment-{seed_data.envs.db.id}"
    assert await load_removal(db, fancy, node_id, user.id) == ()
    change = await remove_access(db, fancy, node_id, user.id, actor=actor_of(seed_data))
    assert (change.changed, change.notice) == (False, STALE_NOTICE)
    assert await load_removal(db, fancy, "environment-987654", user.id) is None
    assert await audit_entries(db, "grant.remove") == []
