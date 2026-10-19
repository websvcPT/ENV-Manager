"""Helpers shared by the web tests of environment pages: htmx requests, ids and header checks."""

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import Deployment, Environment, Service
from app.seeds import EDITOR_ENVIRONMENT_NAME

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
    http: httpx.AsyncClient, url: str, data: Mapping[str, object] | None = None
) -> httpx.Response:
    """Submit a form the way htmx does, with the CSRF header."""
    await http.get("/healthz")
    return await http.post(
        url, data=data or {}, headers={**HTMX, "X-CSRFToken": http.cookies[CSRF_COOKIE_NAME]}
    )


def assert_never_cached(response: httpx.Response) -> None:
    """Every page and fragment that can show values is uncacheable and varies on htmx."""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"


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
