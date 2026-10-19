"""The lookups of a structure item with its parents, on the seeded structure."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.seeds import SeedData
from app.services.structure_lookup import (
    deployment_with_project,
    environment_chain,
    service_with_deployment,
)
from app.tests.grant_helpers import count_statements

MISSING_ID = 999_999


async def test_a_service_comes_with_its_deployment_and_project_name(
    db: AsyncSession, seed_data: SeedData
) -> None:
    chain = await service_with_deployment(db, seed_data.services.staging_web.id)
    assert chain is not None
    assert chain.service.id == seed_data.services.staging_web.id
    assert chain.deployment.id == seed_data.deployments.fancy_staging.id
    assert chain.project_name == seed_data.projects.fancy.name
    service, deployment, project_name = chain
    assert (service.name, deployment.name, project_name) == ("web", "staging", "Fancy Proj. Name")


async def test_services_of_one_name_in_two_deployments_keep_their_own_deployment(
    db: AsyncSession, seed_data: SeedData
) -> None:
    prod = await service_with_deployment(db, seed_data.services.web.id)
    staging = await service_with_deployment(db, seed_data.services.staging_web.id)
    assert prod is not None and staging is not None
    assert prod.service.name == staging.service.name == "web"
    assert prod.deployment.name == "prod"
    assert staging.deployment.name == "staging"


async def test_a_service_in_another_project_names_that_project(
    db: AsyncSession, seed_data: SeedData
) -> None:
    chain = await service_with_deployment(db, seed_data.services.api.id)
    assert chain is not None
    assert chain.deployment.id == seed_data.deployments.billing_main.id
    assert chain.project_name == "billing"


async def test_an_environment_comes_with_its_service_and_deployment(
    db: AsyncSession, seed_data: SeedData
) -> None:
    chain = await environment_chain(db, seed_data.envs.staging_web.id)
    assert chain is not None
    assert chain.environment.id == seed_data.envs.staging_web.id
    assert chain.service.id == seed_data.services.staging_web.id
    assert chain.deployment.id == seed_data.deployments.fancy_staging.id
    environment, service, deployment = chain
    assert (environment.name, service.name, deployment.name) == (".env.web", "web", "staging")


async def test_a_deployment_comes_with_its_project_name(
    db: AsyncSession, seed_data: SeedData
) -> None:
    chain = await deployment_with_project(db, seed_data.deployments.billing_legacy.id)
    assert chain is not None
    deployment, project_name = chain
    assert (deployment.name, project_name) == ("legacy", "billing")


async def test_a_missing_id_is_none_for_every_lookup(db: AsyncSession) -> None:
    assert await service_with_deployment(db, MISSING_ID) is None
    assert await environment_chain(db, MISSING_ID) is None
    assert await deployment_with_project(db, MISSING_ID) is None


async def test_each_lookup_is_one_statement(
    db: AsyncSession, seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    with count_statements(seeded_database) as statements:
        await service_with_deployment(db, seed_data.services.web.id)
        await environment_chain(db, seed_data.envs.web.id)
        await deployment_with_project(db, seed_data.deployments.fancy_prod.id)
    assert len(statements) == 3
