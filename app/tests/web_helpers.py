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
    """The page body without the sidebar, the top bar and the compact breadcrumb."""
    body = page.split("<main", 1)[1].split("</main>", 1)[0]
    return re.sub(r'<nav [^>]*data-breadcrumb="compact".*?</nav>', "", body, flags=re.DOTALL)


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


def current_section(page: str) -> str | None:
    """The address of the open section link under the header, None when none is marked."""
    nav = re.search(r'<nav aria-label="Sections".*?</nav>', page, re.DOTALL)
    assert nav is not None
    current = re.findall(r'<a href="([^"]+)" aria-current="page"', nav.group(0))
    assert len(current) <= 1
    return current[0] if current else None


@dataclass(frozen=True)
class ItemHeader:
    """The visible text of the header of a project, deployment, service or environment page.

    ``subtitle`` is the line of a project (None on the other types). ``location`` maps the key of
    each path line (``root``, ``target``) to its text.
    """

    type: str
    title: str
    subtitle: str | None
    details: str | None
    location: Mapping[str, str]


def _text(markup: str) -> str:
    """The text of a piece of markup with tags removed and spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", markup)).split())


def item_header_of(page: str) -> ItemHeader:
    """The item header of a page: its type, title, subtitle, path lines and details line."""
    header = re.search(
        r'<section data-fragment="page-header" data-item-type="(\w+)".*?</section>',
        page,
        re.DOTALL,
    )
    assert header is not None, "no item header on the page"
    markup = header.group(0)
    title = re.search(r"<h1[^>]*>(.*?)</h1>", markup, re.DOTALL)
    subtitle = re.search(r'<p class="mt-1.5 text-ink2">(.*?)</p>', markup, re.DOTALL)
    details = re.search(r"<div data-header-details[^>]*>(.*?)</div>", markup, re.DOTALL)
    assert title is not None
    lines = re.findall(r'<p data-location="(\w+)">(.*?)</p>', markup, re.DOTALL)
    return ItemHeader(
        header.group(1),
        _text(title.group(1)),
        _text(subtitle.group(1)) if subtitle else None,
        _text(details.group(1)) if details else None,
        {key: _text(line) for key, line in lines},
    )


@dataclass(frozen=True)
class Crumb:
    """One item of a breadcrumb: its label, its type line (None without one) and its link."""

    label: str
    type: str | None
    href: str | None


def breadcrumb_of(page: str, compact: bool = False) -> list[Crumb]:
    """The items of the breadcrumb of a page, the separators left out.

    The last item is the current page and has no link. ``compact`` reads the phone version (the
    parent as a back link and the current item) instead of the full trail.
    """
    kind = "compact" if compact else "full"
    nav = re.search(rf'<nav [^>]*data-breadcrumb="{kind}".*?</nav>', page, re.DOTALL)
    assert nav is not None, f"no {kind} breadcrumb on the page"
    crumbs = []
    for item in re.findall(r"<li(?![^>]*aria-hidden)[^>]*>.*?</li>", nav.group(0), re.DOTALL):
        label = re.search(r"<span data-crumb-label[^>]*>(.*?)</span>", item, re.DOTALL)
        kind_line = re.search(r"<span data-crumb-type[^>]*>(.*?)</span>", item, re.DOTALL)
        link = re.search(r'<a href="([^"]+)"', item)
        assert label is not None
        crumbs.append(
            Crumb(
                _text(label.group(1)),
                _text(kind_line.group(1)) if kind_line else None,
                link.group(1) if link else None,
            )
        )
    return crumbs


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


def without_location(markup: str) -> str:
    """The page without the location lines of its item header, which name the deployment root.

    Screens that print fixed, path-free copy for every problem assert on this, so the header
    (which shows the root only to a caller with access to the whole deployment) stays out of it.
    """
    return re.sub(r"<div data-item-location.*?</div>", "", markup, flags=re.DOTALL)
