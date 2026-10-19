"""Lookups of one structure item together with the parents its callers read.

The deploy engine, the API, the post-deploy settings, the dist check and the item pages all need
a service with its deployment, or an environment with its service and deployment. They ask here
instead of joining the structure tables each time. Every function answers None for an id that
does not exist; whether that is a refusal, a 404 or a skipped step is the caller's decision, and
none of them checks what a caller may see (that is the scope engine's job).
"""

from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deployment, Environment, Project, Service


class ServiceChain(NamedTuple):
    """A service with its deployment and the name of the deployment's project."""

    service: Service
    deployment: Deployment
    project_name: str


class EnvironmentChain(NamedTuple):
    """An environment with its service, the service's deployment and its project's name."""

    environment: Environment
    service: Service
    deployment: Deployment
    project_name: str


class DeploymentChain(NamedTuple):
    """A deployment with the name of its project."""

    deployment: Deployment
    project_name: str


async def service_with_deployment(db: AsyncSession, service_id: int) -> ServiceChain | None:
    """The service, its deployment and its project name, or None when there is no such service."""
    row = (
        await db.execute(
            select(Service, Deployment, Project.name)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .join(Project, Project.id == Deployment.project_id)
            .where(Service.id == service_id)
        )
    ).one_or_none()
    return ServiceChain(row[0], row[1], row[2]) if row is not None else None


async def environment_chain(db: AsyncSession, environment_id: int) -> EnvironmentChain | None:
    """The environment with its service, deployment and project name, or None for a missing file."""
    row = (
        await db.execute(
            select(Environment, Service, Deployment, Project.name)
            .join(Service, Service.id == Environment.service_id)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .join(Project, Project.id == Deployment.project_id)
            .where(Environment.id == environment_id)
        )
    ).one_or_none()
    return EnvironmentChain(row[0], row[1], row[2], row[3]) if row is not None else None


async def deployment_with_project(db: AsyncSession, deployment_id: int) -> DeploymentChain | None:
    """The deployment and its project name, or None when there is no such deployment."""
    row = (
        await db.execute(
            select(Deployment, Project.name)
            .join(Project, Project.id == Deployment.project_id)
            .where(Deployment.id == deployment_id)
        )
    ).one_or_none()
    return DeploymentChain(row[0], row[1]) if row is not None else None
