"""Tests for the login and logout routes and the default-deny route check."""

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from sqlalchemy import select
from starlette.routing import BaseRoute

from app.api.dependencies import require_api_token
from app.config import get_settings
from app.core.cors import is_api_path
from app.core.crypto import generate_master_key
from app.core.csrf import CSRF_COOKIE_NAME
from app.core.sessions import SESSION_COOKIE_NAME
from app.main import create_app
from app.models import AuditEntry, User, UserGroup, UserSession
from app.services.audit import AuditActor
from app.services.users import set_user_active
from app.tests.auth_helpers import PASSWORD, make_user
from app.web.dependencies import require_auth, require_group

PUBLIC_PATHS = {"/healthz", "/login", "/login/totp"}
PUBLIC_PREFIXES = ("/static",)
COOKIE_AUTH = require_auth
BEARER_AUTH = require_api_token


def _auth_calls(dependant: Dependant) -> set[Callable[..., Any]]:
    """The authentication dependencies a route's dependency tree includes."""
    found: set[Callable[..., Any]] = set()
    if dependant.call in (COOKIE_AUTH, BEARER_AUTH) and dependant.call is not None:
        found.add(dependant.call)
    for child in dependant.dependencies:
        found |= _auth_calls(child)
    return found


def _walk(
    routes: Sequence[BaseRoute], prefix: str, inherited: set[Callable[..., Any]]
) -> Iterator[tuple[str, set[Callable[..., Any]]]]:
    """Yield each route's full path and the authentication dependencies it carries.

    FastAPI keeps included routers as lazy wrapper objects; they are unwrapped here so routes
    from included routers are checked too. A wrapper that cannot be read yields an empty path
    with no dependencies, which the caller reports as unprotected, so a FastAPI change fails the
    test loudly.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            context = route.include_context  # type: ignore[attr-defined]
            router_calls = set(inherited)
            for dependency in context.dependencies:
                router_calls |= _auth_calls(get_dependant(path="", call=dependency.dependency))
            yield from _walk(original.routes, prefix + context.prefix, router_calls)
        elif isinstance(route, APIRoute):
            yield prefix + route.path, inherited | _auth_calls(route.dependant)
        else:
            yield prefix + getattr(route, "path", ""), set()


def routes_without_auth(application: FastAPI) -> list[str]:
    """Paths of routes that do not authenticate the way their prefix demands.

    A page route needs the cookie session dependency. An ``/api/`` route needs the bearer token
    dependency and must not use the cookie one. The public allow list is exempt.
    """
    missing: list[str] = []
    for path, calls in _walk(application.routes, "", set()):
        if path in PUBLIC_PATHS or (path and path.startswith(PUBLIC_PREFIXES)):
            continue
        allowed = calls == {BEARER_AUTH} if is_api_path(path) else COOKIE_AUTH in calls
        if not allowed:
            missing.append(path)
    return missing


def test_every_application_route_authenticates_or_is_public() -> None:
    """Default deny: a new route without the auth dependency of its prefix fails this test."""
    assert routes_without_auth(create_app()) == []


def test_the_api_routes_are_checked() -> None:
    """The route walk reaches the API routes, so the check above covers them."""
    paths = {path for path, _ in _walk(create_app().routes, "", set())}
    assert "/api/v1/services/{service_id}/deploy" in paths
    assert "/api/v1/services/{service_id}/deployments/last" in paths


def test_check_detects_a_route_without_authentication() -> None:
    """The enumerating check itself flags unprotected routes and accepts protected ones."""
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()

    @router.get("/open")
    async def open_route() -> dict[str, str]:
        return {}

    @router.get("/closed", dependencies=[Depends(require_auth)])
    async def closed_route() -> dict[str, str]:
        return {}

    @router.get("/grouped", dependencies=[Depends(require_group(UserGroup.ADMIN))])
    async def grouped_route() -> dict[str, str]:
        return {}

    @router.get("/page-with-token", dependencies=[Depends(require_api_token)])
    async def page_with_token() -> dict[str, str]:
        return {}

    application.include_router(router)
    assert routes_without_auth(application) == ["/open", "/page-with-token"]


def test_check_demands_the_bearer_dependency_on_api_routes() -> None:
    """An API route needs the bearer dependency and refuses the cookie one, however it is wired."""
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()

    @router.get("/api/v1/open")
    async def open_route() -> dict[str, str]:
        return {}

    @router.get("/api/v1/cookie", dependencies=[Depends(require_auth)])
    async def cookie_route() -> dict[str, str]:
        return {}

    @router.get("/api/v1/grouped", dependencies=[Depends(require_group(UserGroup.ADMIN))])
    async def grouped_route() -> dict[str, str]:
        return {}

    @router.get("/api/v1/both", dependencies=[Depends(require_api_token), Depends(require_auth)])
    async def both_route() -> dict[str, str]:
        return {}

    @router.get("/api/v1/bearer", dependencies=[Depends(require_api_token)])
    async def bearer_route() -> dict[str, str]:
        return {}

    inherited = APIRouter()

    @inherited.get("/api/v2/inherited")
    async def inherited_route() -> dict[str, str]:
        return {}

    application.include_router(router)
    application.include_router(inherited, dependencies=[Depends(require_api_token)])
    assert routes_without_auth(application) == [
        "/api/v1/open",
        "/api/v1/cookie",
        "/api/v1/grouped",
        "/api/v1/both",
    ]


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    """A client on the real application with its lifespan running on a temporary data folder."""
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    get_settings.cache_clear()
    application = create_app()
    async with application.router.lifespan_context(application):
        async with application.state.session_factory() as db:
            db.add(make_user("alice"))
            await db.commit()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, application
    get_settings.cache_clear()


async def _post(http: httpx.AsyncClient, path: str, data: dict[str, str]) -> httpx.Response:
    """POST a form with the CSRF token the application issued."""
    await http.get("/healthz")
    token = http.cookies[CSRF_COOKIE_NAME]
    return await http.post(path, data={**data, "csrf_token": token})


async def test_login_sets_a_hardened_session_cookie(
    client: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    """Valid credentials redirect and set an HttpOnly, SameSite session cookie."""
    http, _ = client
    credentials = {"identifier": "alice@example.test", "password": PASSWORD}
    response = await _post(http, "/login", credentials)
    assert response.status_code == 303
    cookie = next(
        value
        for name, value in response.headers.multi_items()
        if name == "set-cookie" and value.startswith(SESSION_COOKIE_NAME)
    )
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


async def test_failed_login_is_generic_and_locked_login_reports_time(
    client: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    """Bad credentials answer 401 with a generic card, then repeated failures answer 429."""
    http, _ = client
    wrong = {"identifier": "alice@example.test", "password": "wrong"}
    bad = await _post(http, "/login", wrong)
    assert bad.status_code == 401
    assert "Invalid credentials" in bad.text
    for _ in range(4):
        last = await _post(http, "/login", wrong)
    assert last.status_code == 429
    assert int(last.headers["retry-after"]) > 0


async def test_login_without_csrf_token_is_refused(
    client: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    """The login path is always CSRF checked."""
    http, _ = client
    credentials = {"identifier": "alice@example.test", "password": PASSWORD}
    response = await http.post("/login", data=credentials)
    assert response.status_code == 403


async def test_logout_requires_a_session_and_ends_it(
    client: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    """Logout is refused without a session; with one it deletes it and audits it."""
    http, application = client
    anonymous = await http.post("/logout")
    assert anonymous.status_code == 401
    await _post(http, "/login", {"identifier": "alice@example.test", "password": PASSWORD})
    assert SESSION_COOKIE_NAME in http.cookies
    response = await http.post("/logout", headers={"X-CSRFToken": http.cookies[CSRF_COOKIE_NAME]})
    assert response.status_code == 303
    async with application.state.session_factory() as db:
        assert (await db.execute(select(UserSession))).first() is None
        actions = [entry.action for entry in (await db.execute(select(AuditEntry))).scalars()]
    assert actions == ["login.success", "logout"]


async def test_disabled_user_session_stops_working(
    client: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    """After a user is disabled, the cookie they held no longer authenticates."""
    http, application = client
    await _post(http, "/login", {"identifier": "alice@example.test", "password": PASSWORD})
    async with application.state.session_factory() as db:
        user = (await db.execute(select(User))).scalar_one()
        await set_user_active(db, user, False, actor=AuditActor.cli())
    response = await http.post("/logout", headers={"X-CSRFToken": http.cookies[CSRF_COOKIE_NAME]})
    assert response.status_code == 401
