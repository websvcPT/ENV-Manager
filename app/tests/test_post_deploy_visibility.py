"""Who sees the post-deploy status, output and command text, on every page that can show them."""

from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.models import (
    Deployment,
    DeployRun,
    HookType,
    PostDeployStatus,
    Service,
    ServiceHook,
)
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX

COMMAND_MARKER = "COMMAND-MARKER-4471"
OUTPUT_MARKER = "OUTPUT-MARKER-9921"
REASON_MARKER = "REASON-MARKER-6180"


@dataclass(frozen=True)
class Targets:
    """The services and deployments the visibility checks read."""

    staging_web: int
    staging: int
    mysql: int
    prod: int


@pytest.fixture
async def marked(web_app: FastAPI) -> Targets:
    """Give two services a distinctive command and mark every run of theirs as failed.

    The failed runs carry an exit code, an output and a reason, each with its own marker.
    """
    async with web_app.state.session_factory() as db:
        rows = (
            await db.execute(
                select(Deployment.id, Deployment.name, Service.id, Service.name).join(
                    Service, Service.deployment_id == Deployment.id
                )
            )
        ).all()
        found = {(dep, svc): (dep_id, svc_id) for dep_id, dep, svc_id, svc in rows}
        targets = Targets(
            staging_web=found[("staging", "web")][1],
            staging=found[("staging", "web")][0],
            mysql=found[("prod", "mysql")][1],
            prod=found[("prod", "mysql")][0],
        )
        for service_id in (targets.staging_web, targets.mysql):
            hook = await db.scalar(
                select(ServiceHook).where(
                    ServiceHook.service_id == service_id, ServiceHook.type == HookType.POST_DEPLOY
                )
            )
            command = f"docker compose up -d --no-deps --label marker={COMMAND_MARKER} x"
            if hook is None:
                db.add(
                    ServiceHook(service_id=service_id, type=HookType.POST_DEPLOY, commands=command)
                )
            else:
                hook.commands = command
            await db.execute(
                update(DeployRun)
                .where(DeployRun.service_id == service_id)
                .values(
                    post_deploy_status=PostDeployStatus.FAILED,
                    post_deploy_exit_code=41,
                    post_deploy_output=OUTPUT_MARKER,
                    post_deploy_reason=REASON_MARKER,
                )
            )
        await db.commit()
    return targets


def pages_of(targets: Targets, user: str) -> list[str]:
    """The URLs a user can reach that show post-deploy information."""
    scoped = user in {"env_scoped_admin", "env_scoped_viewer"}
    if scoped or user == "deployment_admin":
        service, deployment = targets.mysql, targets.prod
    else:
        service, deployment = targets.staging_web, targets.staging
    urls = [
        f"/services/{service}",
        f"/services/{service}/postdeploy",
        f"/services/{service}/history",
        f"/deployments/{deployment}/history",
    ]
    return urls if scoped else [*urls, f"/deployments/{deployment}"]


async def read(client: httpx.AsyncClient, urls: list[str]) -> dict[str, str]:
    """The body of each URL; every one must answer 200."""
    bodies: dict[str, str] = {}
    for url in urls:
        fragment = url.endswith(("/postdeploy", "/history"))
        response = await client.get(url, headers=HTMX if fragment else {})
        assert response.status_code == 200, url
        bodies[url] = response.text
    return bodies


@pytest.mark.parametrize("user", ["admin", "project_admin", "deployment_admin", "env_scoped_admin"])
async def test_admins_read_the_output_exit_code_and_reason_but_never_the_command(
    web_client: httpx.AsyncClient, marked: Targets, user: str
) -> None:
    await sign_in(web_client, user)
    bodies = await read(web_client, pages_of(marked, user))
    joined = "\n".join(bodies.values())
    assert COMMAND_MARKER not in joined and "docker compose up" not in joined
    assert "Command settings" not in joined
    service_page = bodies[pages_of(marked, user)[0]]
    assert OUTPUT_MARKER in service_page and REASON_MARKER in service_page
    assert "exit code 41" in service_page
    assert 'data-status="failed"' in service_page


@pytest.mark.parametrize("user", ["viewer", "env_scoped_viewer"])
async def test_viewers_read_the_status_but_never_output_exit_code_reason_or_command(
    web_client: httpx.AsyncClient, marked: Targets, user: str
) -> None:
    await sign_in(web_client, user)
    bodies = await read(web_client, pages_of(marked, user))
    joined = "\n".join(bodies.values())
    for secret in (COMMAND_MARKER, OUTPUT_MARKER, REASON_MARKER, "exit code", "docker compose"):
        assert secret not in joined, secret
    assert "Command settings" not in joined
    service_page = bodies[pages_of(marked, user)[0]]
    assert 'data-status="failed"' in service_page
    assert "Output and command text are not available for the Viewer group." in service_page


async def test_the_superadmin_alone_reads_the_command_next_to_the_output(
    web_client: httpx.AsyncClient, marked: Targets
) -> None:
    await sign_in(web_client, "root")
    bodies = await read(web_client, pages_of(marked, "root"))
    service_page = bodies[f"/services/{marked.staging_web}"]
    assert COMMAND_MARKER in service_page and OUTPUT_MARKER in service_page
    assert REASON_MARKER in service_page and "exit code 41" in service_page
    deployment_page = bodies[f"/deployments/{marked.staging}"]
    assert COMMAND_MARKER not in deployment_page and "Post-deploy exit code 41" in deployment_page
    assert OUTPUT_MARKER not in deployment_page


@pytest.mark.parametrize("user", ["root", "admin", "viewer"])
async def test_a_command_that_is_still_running_reads_running_on_every_page(
    web_app: FastAPI, web_client: httpx.AsyncClient, marked: Targets, user: str
) -> None:
    async with web_app.state.session_factory() as db:
        await db.execute(
            update(DeployRun)
            .where(DeployRun.service_id == marked.staging_web)
            .values(
                post_deploy_status=PostDeployStatus.RUNNING,
                post_deploy_exit_code=None,
                post_deploy_output=None,
                post_deploy_reason=None,
            )
        )
        await db.commit()
    await sign_in(web_client, user)
    bodies = await read(web_client, pages_of(marked, user))
    for url, body in bodies.items():
        assert 'data-status="running"' in body, url
        assert "Running" in body and OUTPUT_MARKER not in body
