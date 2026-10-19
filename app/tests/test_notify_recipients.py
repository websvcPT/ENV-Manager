"""Tests for who a notification goes to: superadmins, project admins by reach, and the choices."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import User, UserGroup
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


def test_an_address_is_used_once_whatever_its_case_and_the_first_recipient_wins() -> None:
    """Two roles that lead to one address write to it once."""
    first = Recipient("Alice@Example.com", frozenset({1}))
    second = Recipient("alice@example.com", None)
    other = Recipient("bob@example.com")
    assert unique_by_address([first, second, other]) == [first, other]
