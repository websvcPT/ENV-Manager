"""Helpers shared by the web tests.

Htmx requests and form posts, ids of the seeded environments, page readers, version helpers and the
seeded deployments staged on real folders with a fake docker.
"""

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import (
    Deployment,
    Environment,
    EnvironmentVersion,
    HookType,
    Project,
    Service,
    ServiceHook,
)
from app.seeds import EDITOR_ENVIRONMENT_NAME
from app.services.versions import create_version
from app.tests.dist_helpers import relocate_deployments
from app.tests.fake_docker import FakeDocker

HTMX = {"HX-Request": "true"}


@dataclass(frozen=True)
class EnvironmentIds:
    """Ids of the seeded environments the web tests use."""

    editor: int
    db: int
    ca: int
    api: int
    tuning: int


async def environment_ids(web_app: FastAPI) -> EnvironmentIds:
    """The seeded environment ids of the web application."""
    async with web_app.state.session_factory() as db:
        rows = (
            await db.execute(
                select(Deployment.name, Service.name, Environment.name, Environment.id)
                .join(Service, Service.deployment_id == Deployment.id)
                .join(Environment, Environment.service_id == Service.id)
            )
        ).all()
    found = {
        (deployment, service, name): environment for deployment, service, name, environment in rows
    }
    return EnvironmentIds(
        editor=found[("prod", "mysql", EDITOR_ENVIRONMENT_NAME)],
        db=found[("prod", "mysql", ".env.db")],
        ca=found[("staging", "web", "web-ca.pem")],
        api=found[("main", "api", ".env.api")],
        tuning=found[("staging", "web", ".env.web.tuning")],
    )


async def post(
    http: httpx.AsyncClient,
    url: str,
    data: Mapping[str, object] | None = None,
    files: Mapping[str, tuple[str, bytes]] | None = None,
) -> httpx.Response:
    """Submit a form the way htmx does, with the CSRF header (and files for a multipart one)."""
    await http.get("/healthz")
    return await http.post(
        url,
        data=data or {},
        files=files,
        headers={**HTMX, "X-CSRFToken": http.cookies[CSRF_COOKIE_NAME]},
    )


def assert_never_cached(response: httpx.Response) -> None:
    """Every page and fragment that can show values is uncacheable and varies on htmx."""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"


def events_of(response: httpx.Response) -> dict[str, object]:
    """The client events an HX-Trigger header fires."""
    parsed: dict[str, object] = json.loads(response.headers["hx-trigger"])
    return parsed


def main_of(page: str) -> str:
    """The page body without the sidebar and top bar."""
    return page.split("<main", 1)[1].split("</main>", 1)[0]


def hx_vals(page: str, path: str) -> dict[str, object]:
    """The hx-vals of the control that posts to ``path``, as the browser would send them."""
    found = re.search(rf'hx-post="{re.escape(path)}"[^>]*?hx-vals="([^"]*)"', page)
    assert found, path
    values: dict[str, object] = json.loads(html.unescape(found.group(1)))
    return values


def without_csrf_token(page: str) -> str:
    """The page without the request token its body carries, in the htmx headers and in the hidden
    field of a form (the log out form of the account menu).

    The token is random hex, so a search of a whole page for a short digit string such as a port
    would now and then find it there.
    """
    page = re.sub(r"hx-headers:inherited='[^']*'", "", page)
    return re.sub(r'<input type="hidden" name="csrf_token" value="[^"]*">', "", page)


def section_links(page: str) -> list[str]:
    """Visible text of the section links under the header."""
    nav = re.search(r'<nav aria-label="Sections".*?</nav>', page, re.DOTALL)
    assert nav is not None
    anchors = re.findall(r"<a [^>]*>(.*?)</a>", nav.group(0), re.DOTALL)
    return [re.sub(r"<[^>]+>", "", anchor).strip() for anchor in anchors]


async def add_version(web_app: FastAPI, environment: int, text: str) -> None:
    """Save a text as the next version of an environment."""
    async with web_app.state.session_factory() as db:
        await create_version(db, web_app.state.crypto, environment, text, author=None)
        await db.commit()


async def version_numbers(web_app: FastAPI, environment: int) -> list[int]:
    """The saved version numbers of an environment, oldest first."""
    async with web_app.state.session_factory() as db:
        return list(
            (
                await db.execute(
                    select(EnvironmentVersion.number)
                    .where(EnvironmentVersion.environment_id == environment)
                    .order_by(EnvironmentVersion.number)
                )
            ).scalars()
        )


@dataclass(frozen=True)
class StagedDeployments:
    """The seeded structure moved onto real folders, with a fake docker and the ids by name."""

    roots: dict[str, Path]
    docker: FakeDocker
    projects: dict[str, int]
    deployments: dict[str, int]
    services: dict[str, int]
    environments: dict[str, int]


async def stage_deployments(
    web_app: FastAPI, tmp_path: Path, hook_command: str
) -> StagedDeployments:
    """Put the seeded deployments on a real folder so a deploy writes real files.

    Dist links and file owners are removed, the production MySQL service stores ``hook_command``
    as its post-deploy command and the application's docker CLI is a fake script, so the command
    that runs is a real (fake) process whose calls the tests read back.
    """
    async with web_app.state.session_factory() as db:
        roots = await relocate_deployments(db, tmp_path / "host")
        for environment in (await db.execute(select(Environment))).scalars():
            environment.dist_path = None
        for deployment in (await db.execute(select(Deployment))).scalars():
            deployment.file_owner = None
            deployment.file_group = None
        mysql = await db.scalar(select(Service).where(Service.name == "mysql"))
        assert mysql is not None
        db.add(ServiceHook(service_id=mysql.id, type=HookType.POST_DEPLOY, commands=hook_command))
        await db.commit()
        projects = dict((await db.execute(select(Project.name, Project.id))).all())
        deployments = dict((await db.execute(select(Deployment.name, Deployment.id))).all())
        services = {
            f"{deployment}/{service}": identifier
            for deployment, service, identifier in (
                await db.execute(
                    select(Deployment.name, Service.name, Service.id).join(
                        Service, Service.deployment_id == Deployment.id
                    )
                )
            ).all()
        }
        environments = {
            f"{service}/{name}": identifier
            for service, name, identifier in (
                await db.execute(
                    select(Service.name, Environment.name, Environment.id).join(
                        Service, Service.id == Environment.service_id
                    )
                )
            ).all()
        }
    docker = FakeDocker(tmp_path / "fake-docker")
    web_app.state.docker_cli = docker.cli()
    return StagedDeployments(roots, docker, projects, deployments, services, environments)
