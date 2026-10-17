"""Tests for the login and logout routes and the default-deny route check."""

from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from sqlalchemy import select
from starlette.routing import BaseRoute

from app.config import get_settings
from app.core.crypto import generate_master_key
from app.core.csrf import CSRF_COOKIE_NAME
from app.core.sessions import SESSION_COOKIE_NAME
from app.main import create_app
from app.models import AuditActorType, AuditEntry, User, UserGroup, UserSession
from app.services.users import set_user_active
from app.tests.auth_helpers import PASSWORD, make_user
from app.web.dependencies import require_auth, require_group

PUBLIC_PATHS = {"/healthz", "/login"}
PUBLIC_PREFIXES = ("/static",)


def _uses_auth(dependant: Dependant) -> bool:
    """Whether a route's dependency tree includes the authentication dependency."""
    if dependant.call is require_auth:
        return True
    return any(_uses_auth(child) for child in dependant.dependencies)


def _walk(
    routes: Sequence[BaseRoute], prefix: str, inherited_auth: bool
) -> Iterator[tuple[str, bool]]:
    """Yield each route's full path and whether it authenticates.

    FastAPI keeps included routers as lazy wrapper objects; they are unwrapped here so routes
    from included routers are checked too. A wrapper that cannot be read yields an empty path,
    which the caller reports as unprotected, so a FastAPI change fails the test loudly.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            context = route.include_context  # type: ignore[attr-defined]
            router_auth = inherited_auth or any(
                _uses_auth(get_dependant(path="", call=dependency.dependency))
                for dependency in context.dependencies
            )
            yield from _walk(original.routes, prefix + context.prefix, router_auth)
        elif isinstance(route, APIRoute):
            yield prefix + route.path, inherited_auth or _uses_auth(route.dependant)
        else:
            yield prefix + getattr(route, "path", ""), False


def routes_without_auth(application: FastAPI) -> list[str]:
    """Paths of routes that neither authenticate nor sit on the public allow list."""
    missing: list[str] = []
    for path, authenticated in _walk(application.routes, "", False):
        if path in PUBLIC_PATHS or (path and path.startswith(PUBLIC_PREFIXES)):
            continue
        if not authenticated:
            missing.append(path)
    return missing


def test_every_application_route_authenticates_or_is_public() -> None:
    """Default deny: a new route without an auth dependency fails this test."""
    assert routes_without_auth(create_app()) == []


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

    application.include_router(router)
    assert routes_without_auth(application) == ["/open"]


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
    response = await _post(http, "/login", {"identifier": "alice", "password": PASSWORD})
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
    """Bad credentials answer 401 with no detail, then repeated failures answer 429."""
    http, _ = client
    bad = await _post(http, "/login", {"identifier": "alice", "password": "wrong"})
    assert (bad.status_code, bad.json()) == (401, {"status": "invalid"})
    for _ in range(4):
        last = await _post(http, "/login", {"identifier": "alice", "password": "wrong"})
    assert last.status_code == 429
    assert int(last.headers["retry-after"]) > 0


async def test_login_without_csrf_token_is_refused(
    client: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    """The login path is always CSRF checked."""
    http, _ = client
    response = await http.post("/login", data={"identifier": "alice", "password": PASSWORD})
    assert response.status_code == 403


async def test_logout_requires_a_session_and_ends_it(
    client: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    """Logout is refused without a session; with one it deletes it and audits it."""
    http, application = client
    anonymous = await http.post("/logout")
    assert anonymous.status_code == 401
    await _post(http, "/login", {"identifier": "alice", "password": PASSWORD})
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
    await _post(http, "/login", {"identifier": "alice", "password": PASSWORD})
    async with application.state.session_factory() as db:
        user = (await db.execute(select(User))).scalar_one()
        await set_user_active(db, user, False, actor_type=AuditActorType.CLI)
    response = await http.post("/logout", headers={"X-CSRFToken": http.cookies[CSRF_COOKIE_NAME]})
    assert response.status_code == 401
