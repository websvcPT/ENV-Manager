"""Tests for who a notification goes to: superadmins, project admins by reach, and the choices."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import Project, User, UserGroup
from app.seeds import SeedData
from app.services.notify_catalog import Recipients
from app.services.notify_recipients import (
    ProjectItem,
    Recipient,
    project_admin_recipients,
    resolve_recipients,
    superadmin_recipients,
    unique_by_address,
)


def mysql_item(seed: SeedData, *environment_ids: int) -> ProjectItem:
    """The production MySQL service of the rentals project with the given files."""
    return ProjectItem(
        seed.projects.rentals.id,
        seed.deployments.rentals_prod.id,
        seed.services.mysql.id,
        environment_ids,
    )


def addresses(recipients: list[Recipient]) -> list[str]:
    """The user names behind the addresses (the seed's addresses are name@example.test)."""
    return sorted(item.address.removesuffix("@example.test") for item in recipients)


async def test_system_events_go_to_every_active_superadmin(
    seeded_database: AsyncEngine,
) -> None:
    """Only superadmins, with an address, and a disabled one is left out."""
    async with create_session_factory(seeded_database)() as db:
        assert addresses(await superadmin_recipients(db)) == ["root"]
        for name, active in (("second", True), ("gone", False)):
            db.add(
                User(
                    name=name,
                    email=f"{name}@example.test",
                    password_hash="x",
                    group=UserGroup.SUPERADMIN,
                    is_active=active,
                )
            )
        await db.commit()
        assert addresses(await superadmin_recipients(db)) == ["root", "second"]


async def test_project_admins_are_those_whose_grants_reach_a_file_of_the_event(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Project, deployment and environment grants reach the file; no grant and a viewer do not."""
    envs = seed_data.envs
    async with create_session_factory(seeded_database)() as db:
        found = await project_admin_recipients(db, mysql_item(seed_data, envs.db.id))
        assert addresses(found) == [
            "admin",
            "deployment_admin",
            "env_scoped_admin",
            "project_admin",
        ]
        for recipient in found:
            assert recipient.environment_ids == frozenset({envs.db.id})


async def test_each_admin_is_given_only_the_files_of_the_event_they_can_reach(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """The environment scoped admin holds .env.db only, so the secrets file is not theirs."""
    envs = seed_data.envs
    both = mysql_item(seed_data, envs.db.id, envs.db_secrets.id)
    async with create_session_factory(seeded_database)() as db:
        found = {
            item.address.removesuffix("@example.test"): item.environment_ids
            for item in await project_admin_recipients(db, both)
        }
    everything = frozenset({envs.db.id, envs.db_secrets.id})
    assert found == {
        "admin": everything,
        "project_admin": everything,
        "deployment_admin": everything,
        "env_scoped_admin": frozenset({envs.db.id}),
    }


async def test_an_admin_with_no_grant_reaching_the_files_gets_nothing(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Only the environment scoped admin reaches .env.db; asking for the secrets file leaves
    them out, and a file of another project leaves out the rentals admins."""
    envs = seed_data.envs
    async with create_session_factory(seeded_database)() as db:
        secrets_only = await project_admin_recipients(db, mysql_item(seed_data, envs.db_secrets.id))
        assert "env_scoped_admin" not in addresses(secrets_only)
        billing = ProjectItem(
            seed_data.projects.billing.id,
            seed_data.deployments.billing_main.id,
            seed_data.services.api.id,
            (envs.api.id,),
        )
        assert addresses(await project_admin_recipients(db, billing)) == ["admin"]


async def test_a_disabled_admin_and_one_without_a_usable_address_are_left_out(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Only active admins with a plain address are written to."""
    async with create_session_factory(seeded_database)() as db:
        project_admin = await db.get(User, seed_data.users.project_admin.id)
        deployment_admin = await db.get(User, seed_data.users.deployment_admin.id)
        assert project_admin is not None and deployment_admin is not None
        project_admin.is_active = False
        deployment_admin.email = "not an address"
        await db.commit()
        found = await project_admin_recipients(db, mysql_item(seed_data, seed_data.envs.db.id))
        assert addresses(found) == ["admin", "env_scoped_admin"]


async def test_an_event_with_no_file_reaches_admins_who_cover_the_service(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """With no file named, the admins whose grants include the service are the project's."""
    async with create_session_factory(seeded_database)() as db:
        found = await project_admin_recipients(db, mysql_item(seed_data))
        assert addresses(found) == ["admin", "deployment_admin", "project_admin"]


@pytest.mark.parametrize(
    ("choice", "wanted"),
    [
        (Recipients.SUPERADMINS, ["root"]),
        (
            Recipients.PROJECT_ADMINS,
            ["admin", "deployment_admin", "env_scoped_admin", "project_admin"],
        ),
        (
            Recipients.BOTH,
            ["admin", "deployment_admin", "env_scoped_admin", "project_admin", "root"],
        ),
    ],
)
async def test_the_choice_picks_the_audience_of_an_event_about_a_project(
    seeded_database: AsyncEngine, seed_data: SeedData, choice: Recipients, wanted: list[str]
) -> None:
    """Superadmins, project admins or both."""
    async with create_session_factory(seeded_database)() as db:
        found = await resolve_recipients(db, choice, mysql_item(seed_data, seed_data.envs.db.id))
        assert addresses(found) == wanted


async def test_an_event_about_the_system_goes_to_the_superadmins_whatever_the_choice(
    seeded_database: AsyncEngine,
) -> None:
    """With no item there are no project admins to choose."""
    async with create_session_factory(seeded_database)() as db:
        for choice in Recipients:
            assert addresses(await resolve_recipients(db, choice, None)) == ["root"]


def test_an_address_is_used_once_whatever_its_case_and_keeps_the_first_spelling() -> None:
    """Two roles that lead to one address write to it once, in the place of the first."""
    first = Recipient("Alice@Example.com", frozenset({1}))
    second = Recipient("alice@example.com", frozenset({1}))
    other = Recipient("bob@example.com")
    assert unique_by_address([first, second, other]) == [first, other]


def test_an_address_of_two_roles_is_told_about_the_widest_set_of_files() -> None:
    """Every file beats a set of files, and two sets are joined, whichever comes first."""
    narrow = Recipient("Alice@Example.com", frozenset({1}))
    other_narrow = Recipient("alice@example.com", frozenset({2}))
    everything = Recipient("ALICE@example.com", None)
    assert unique_by_address([narrow, everything]) == [Recipient("Alice@Example.com", None)]
    assert unique_by_address([everything, narrow]) == [everything]
    assert unique_by_address([narrow, other_narrow]) == [
        Recipient("Alice@Example.com", frozenset({1, 2}))
    ]


async def set_extras(engine: AsyncEngine, project_id: int, addresses: list[str]) -> None:
    """Store the extra notification addresses of a project."""
    async with create_session_factory(engine)() as db:
        project = await db.get(Project, project_id)
        assert project is not None
        project.extra_addresses = addresses
        await db.commit()


@pytest.mark.parametrize("choice", list(Recipients))
async def test_the_extra_addresses_of_a_project_are_told_whatever_the_choice(
    seeded_database: AsyncEngine, seed_data: SeedData, choice: Recipients
) -> None:
    """Superadmins, project admins or both, the extras come on top with every file."""
    await set_extras(
        seeded_database, seed_data.projects.rentals.id, ["team@example.org", "pager@corp.local"]
    )
    async with create_session_factory(seeded_database)() as db:
        found = await resolve_recipients(db, choice, mysql_item(seed_data, seed_data.envs.db.id))
    extras = [item for item in found if item.address in ("team@example.org", "pager@corp.local")]
    assert [item.address for item in extras] == ["team@example.org", "pager@corp.local"]
    assert all(item.environment_ids is None for item in extras)
    assert len(found) == len({item.address.lower() for item in found})


async def test_an_event_about_the_system_never_goes_to_the_extra_addresses(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """With no item there is no project, so no extras."""
    await set_extras(seeded_database, seed_data.projects.rentals.id, ["team@example.org"])
    async with create_session_factory(seeded_database)() as db:
        for choice in Recipients:
            assert addresses(await resolve_recipients(db, choice, None)) == ["root"]


async def test_the_extra_addresses_of_another_project_are_not_used(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Each project has its own list."""
    await set_extras(seeded_database, seed_data.projects.billing.id, ["bill@example.org"])
    async with create_session_factory(seeded_database)() as db:
        found = await resolve_recipients(
            db, Recipients.PROJECT_ADMINS, mysql_item(seed_data, seed_data.envs.db.id)
        )
    assert "bill@example.org" not in [item.address for item in found]


async def test_an_extra_address_of_a_scoped_admin_is_told_about_every_file_once(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """The widest set wins, whatever the case of the address and the order of the roles."""
    await set_extras(
        seeded_database, seed_data.projects.rentals.id, ["Env_Scoped_Admin@example.test"]
    )
    item = mysql_item(seed_data, seed_data.envs.db.id, seed_data.envs.db_secrets.id)
    async with create_session_factory(seeded_database)() as db:
        found = await resolve_recipients(db, Recipients.PROJECT_ADMINS, item)
    scoped = [r for r in found if r.address.lower() == "env_scoped_admin@example.test"]
    assert scoped == [Recipient("env_scoped_admin@example.test", None)]


async def test_a_stored_address_that_is_not_a_plain_address_is_left_out(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """A list changed by hand cannot send mail to a name with a display part or a space."""
    await set_extras(
        seeded_database,
        seed_data.projects.rentals.id,
        ["Team <team@example.org>", "bad address@example.org", "good@example.org"],
    )
    async with create_session_factory(seeded_database)() as db:
        found = await resolve_recipients(
            db, Recipients.SUPERADMINS, mysql_item(seed_data, seed_data.envs.db.id)
        )
    assert [item.address for item in found] == ["root@example.test", "good@example.org"]
