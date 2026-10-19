"""A value a post-deploy command printed reaches no page, fragment or role.

A user who holds one file deploys a service whose command prints values of files that user cannot
see. Every page and panel that can show post-deploy information is then read as every role.
"""

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import Deployment, DeployStatus, Environment, HookType, Service, ServiceHook, User
from app.seeds import EDITOR_SECRET_VALUES
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_types import DeployRequest
from app.services.hook_runner import DockerHookRunner
from app.services.permissions import load_user_scope
from app.tests.conftest import sign_in
from app.tests.dist_helpers import link_dist, relocate_deployments
from app.tests.fake_docker import FakeDocker
from app.tests.web_helpers import HTMX, without_csrf_token

VALUES = ("seed-secret", "3306", EDITOR_SECRET_VALUES[3], EDITOR_SECRET_VALUES[2])
OUTPUT_ROLES = ("root", "admin", "project_admin", "deployment_admin", "env_scoped_admin")
STATUS_ONLY_ROLES = ("viewer", "env_scoped_viewer")


@dataclass(frozen=True)
class Targets:
    """The prod mysql service and its deployment."""

    service: int
    deployment: int


@pytest.fixture
async def deployed(web_app: FastAPI, tmp_path: Path) -> Targets:
    """Deploy prod mysql as the user granted only ``.env.db``; its command prints secrets."""
    fake = FakeDocker(tmp_path / "fake")
    fake.behavior(
        f'echo "password seed-secret port 3306 host db {EDITOR_SECRET_VALUES[3]}"; '
        f'echo "{EDITOR_SECRET_VALUES[2]}"'
    )
    async with web_app.state.session_factory() as db:
        await relocate_deployments(db, tmp_path / "host")
        row = (
            await db.execute(
                select(Service.id, Deployment.id)
                .join(Deployment, Deployment.id == Service.deployment_id)
                .where(Deployment.name == "prod", Service.name == "mysql")
            )
        ).one()
        environment = await db.scalar(select(Environment).where(Environment.name == ".env.db"))
        assert environment is not None
        await link_dist(db, environment, None)
        prod = await db.get(Deployment, row[1])
        assert prod is not None
        prod.file_owner = None
        prod.file_group = None
        db.add(
            ServiceHook(
                service_id=row[0], type=HookType.POST_DEPLOY, commands="docker compose config"
            )
        )
        await db.commit()
        user = await db.scalar(select(User).where(User.name == "env_scoped_admin"))
        assert user is not None
        engine = DeployEngine(
            web_app.state.crypto, tmp_path / "locks", hook_runner=DockerHookRunner(fake.cli())
        )
        outcome = await engine.run(
            db,
            await load_user_scope(db, user),
            AuditActor.for_user(user),
            DeployRequest(service_id=row[0]),
        )
    assert outcome.status is DeployStatus.SUCCESS, [e.message for e in outcome.environments]
    assert outcome.post_deploy.output is not None
    assert "****" in outcome.post_deploy.output
    return Targets(service=row[0], deployment=row[1])


def urls_of(targets: Targets) -> list[str]:
    """Every URL that can show post-deploy information of the deployed service."""
    return [
        f"/services/{targets.service}",
        f"/services/{targets.service}/postdeploy",
        f"/services/{targets.service}/history",
        f"/deployments/{targets.deployment}/history",
        f"/deployments/{targets.deployment}",
    ]


async def read_all(client: httpx.AsyncClient, targets: Targets) -> dict[str, str]:
    """The body of every URL, each answering 200."""
    bodies: dict[str, str] = {}
    for url in urls_of(targets):
        fragment = url.endswith(("/postdeploy", "/history"))
        response = await client.get(url, headers=HTMX if fragment else {})
        assert response.status_code == 200, url
        bodies[url] = response.text
    return bodies


@pytest.mark.parametrize("user", [*OUTPUT_ROLES, *STATUS_ONLY_ROLES])
async def test_no_value_the_command_printed_is_on_any_page_for_any_role(
    web_client: httpx.AsyncClient, deployed: Targets, user: str
) -> None:
    await sign_in(web_client, user)
    bodies = await read_all(web_client, deployed)
    for url, body in bodies.items():
        for value in VALUES:
            assert value not in without_csrf_token(body), (user, url, value)
    service_page = bodies[f"/services/{deployed.service}"]
    if user in OUTPUT_ROLES:
        assert "password **** port **** host db ****" in service_page
        assert "host db" in service_page, "a value under four characters is not scrubbed"
    else:
        assert "<pre" not in service_page and "password" not in service_page
