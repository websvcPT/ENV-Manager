"""Helpers of the API tests: a seeded application with deployments on a real folder, a fake docker
and a way to mint tokens for seeded users.

The staging, production and billing deployments are moved onto a temporary folder, the files hold
no owner, the production MySQL service stores a post-deploy command and the docker CLI is the fake
script, so a deploy over HTTP writes real files and runs a real (fake) command.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditEntry,
    User,
)
from app.services.audit import AuditActor
from app.services.permissions import load_user_scope
from app.services.tokens import (
    OPERATION_DEPLOY,
    OPERATION_READ_LAST_DEPLOYMENT,
    create_token,
)
from app.tests.fake_docker import FakeDocker
from app.tests.web_helpers import stage_deployments

HOOK_COMMAND = "docker compose up -d --no-deps mysql"
BOTH_OPERATIONS = (OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT)
CLIENT_ADDRESS = ("198.51.100.7", 4321)


@dataclass(frozen=True)
class ApiWorld:
    """The application, a client of it and the ids and folders the tests need."""

    app: FastAPI
    http: httpx.AsyncClient
    roots: dict[str, Path]
    docker: FakeDocker
    projects: dict[str, int]
    deployments: dict[str, int]
    services: dict[str, int]
    environments: dict[str, int]

    def target(self, deployment: str, name: str) -> Path:
        """The path of a target file below a deployment root."""
        return self.roots[deployment] / "environments" / name

    def session(self) -> AsyncSession:
        """A database session of the application."""
        session: AsyncSession = self.app.state.session_factory()
        return session


def bearer(secret: str) -> dict[str, str]:
    """The header a client sends."""
    return {"Authorization": f"Bearer {secret}"}


@asynccontextmanager
async def build_world(web_app: FastAPI, tmp_path: Path) -> AsyncIterator[ApiWorld]:
    """The seeded application with real folders, a fake docker and an API client."""
    staged = await stage_deployments(web_app, tmp_path, HOOK_COMMAND)
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False, client=CLIENT_ADDRESS)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield ApiWorld(
            web_app,
            http,
            staged.roots,
            staged.docker,
            staged.projects,
            staged.deployments,
            staged.services,
            staged.environments,
        )


async def make_token(
    world: ApiWorld,
    owner: str,
    *,
    operations: tuple[str, ...] = BOTH_OPERATIONS,
    restriction: str = "",
    name: str = "ci",
    expires: str = "",
) -> str:
    """Create a token for a seeded user the way the screen does and return its secret."""
    async with world.session() as db:
        user = await db.scalar(select(User).where(User.name == owner))
        assert user is not None
        created = await create_token(
            db,
            user,
            await load_user_scope(db, user),
            name=name,
            operations=operations,
            restriction=restriction,
            expires=expires,
            actor=AuditActor.for_user(user),
        )
    return created.secret


async def deploy(
    world: ApiWorld,
    secret: str,
    service: int | str,
    body: Any = None,
    *,
    raw: bytes | None = None,
) -> httpx.Response:
    """POST a deploy request: a JSON body, or the raw bytes given."""
    url = f"/api/v1/services/{service}/deploy"
    if raw is not None:
        return await world.http.post(url, content=raw, headers=bearer(secret))
    return await world.http.post(url, json=body if body is not None else {}, headers=bearer(secret))


async def last(world: ApiWorld, secret: str, service: int | str) -> httpx.Response:
    """GET the last deployment of a service."""
    return await world.http.get(
        f"/api/v1/services/{service}/deployments/last", headers=bearer(secret)
    )


async def audit_entries(world: ApiWorld, after: int = 0) -> list[AuditEntry]:
    """The audit entries newer than an id, oldest first."""
    async with world.session() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.id > after).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


async def last_audit_id(world: ApiWorld) -> int:
    """The id of the newest audit entry, to read what a call added."""
    entries = await audit_entries(world)
    return entries[-1].id if entries else 0


def fingerprint(response: httpx.Response) -> tuple[int, bytes, tuple[tuple[str, str], ...]]:
    """Everything of an answer a client could tell apart, except the date."""
    headers = sorted((name, value) for name, value in response.headers.items() if name != "date")
    return response.status_code, response.content, tuple(headers)
