"""Password sign in while single sign-on is on: only superadmins may use it.

Any other account is answered exactly like a wrong password (the same status, the same page,
the same password check), so nothing tells who is a superadmin; the audit entry and the failure
counters tell the real reason to the people who may read them.
"""

import re

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.csrf import CSRF_COOKIE_NAME
from app.core.passwords import verify_password
from app.core.sessions import PENDING_LOGIN_COOKIE_NAME, SESSION_COOKIE_NAME
from app.models import LoginCounter, LoginCounterKind, User
from app.models.types import utc_now
from app.seeds import DEV_PASSWORD, DEV_TOTP_SECRET, seed_email
from app.services import totp
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginStatus, authenticate
from app.tests.sso_helpers import BASE_URL, World
from app.tests.sso_web_helpers import audit_entries, sso_world


@pytest.fixture(autouse=True)
def base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address the application is reached at, before the application starts."""
    monkeypatch.setenv("ENVMANAGER_BASE_URL", BASE_URL)


@pytest.fixture
def world(web_app: FastAPI) -> World:
    """The application's side of a sign in against a fresh fake provider."""
    return sso_world(web_app)


async def post_login(
    http: httpx.AsyncClient, path: str, data: dict[str, str], *, htmx: bool = False
) -> httpx.Response:
    """POST a sign in form with the CSRF field the application issued."""
    await http.get("/healthz")
    form = {**data, "csrf_token": http.cookies[CSRF_COOKIE_NAME]}
    return await http.post(path, data=form, headers={"HX-Request": "true"} if htmx else None)


def credentials(name: str, password: str = DEV_PASSWORD) -> dict[str, str]:
    """The sign in form of a seeded user."""
    return {"identifier": seed_email(name), "password": password}


async def _reasons(web_app: FastAPI) -> list[str]:
    return [
        str(entry.details.get("reason")) for entry in await audit_entries(web_app, "login.failure")
    ]


@pytest.mark.parametrize("name", ["admin", "viewer", "env_scoped_viewer"])
async def test_a_correct_password_of_a_non_superadmin_is_refused_like_a_wrong_one(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World, name: str
) -> None:
    await world.configure()
    correct = await post_login(web_client, "/login", credentials(name))
    wrong = await post_login(web_client, "/login", credentials(name, "not the password"))
    assert correct.status_code == wrong.status_code == 401
    assert correct.text == wrong.text
    assert "Invalid credentials" in correct.text
    assert SESSION_COOKIE_NAME not in web_client.cookies
    assert await _reasons(web_app) == ["password_sign_in_off", "password_sign_in_off"]


async def test_the_refusal_is_counted_like_a_failure(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    for _ in range(5):
        response = await post_login(web_client, "/login", credentials("viewer"), htmx=True)
    assert response.status_code == 429 and "Account temporarily locked" in response.text
    async with web_app.state.session_factory() as db:
        counters = (await db.execute(select(LoginCounter))).scalars().all()
    by_kind = {(counter.kind, counter.subject): counter.failures for counter in counters}
    assert by_kind[(LoginCounterKind.USER, seed_email("viewer"))] == 5
    assert by_kind[(LoginCounterKind.IP, "127.0.0.1")] == 5


async def test_the_refusal_does_not_tell_a_superadmin_from_anyone_else(
    web_client: httpx.AsyncClient, world: World
) -> None:
    """An admin with the right password and a superadmin with a wrong one look the same."""
    await world.configure()
    admin = await post_login(web_client, "/login", credentials("admin"))
    unknown = await post_login(
        web_client, "/login", {"identifier": "nobody@example.test", "password": DEV_PASSWORD}
    )
    superadmin_wrong = await post_login(web_client, "/login", credentials("root", "wrong"))
    statuses = {admin.status_code, unknown.status_code, superadmin_wrong.status_code}
    assert statuses == {401}
    strip = re.compile(r'value="[^"]*@example\.test"')
    pages = {strip.sub("", response.text) for response in (admin, unknown, superadmin_wrong)}
    assert len(pages) == 1


async def test_a_refused_account_still_has_its_password_checked(
    web_client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes after the same hash check a wrong password costs."""
    await world.configure()
    checks: list[str] = []
    real = verify_password

    def counting(password: str, stored_hash: str) -> tuple[bool, str | None]:
        checks.append(stored_hash)
        return real(password, stored_hash)

    monkeypatch.setattr("app.services.login.verify_password", counting)
    await post_login(web_client, "/login", credentials("admin"))
    await post_login(web_client, "/login", credentials("admin", "wrong"))
    assert len(checks) == 2


async def test_a_superadmin_keeps_password_sign_in(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure()
    response = await post_login(web_client, "/login", credentials("root"))
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert (await web_client.get("/projects")).status_code == 200


async def test_a_superadmin_with_two_factor_is_asked_for_the_code(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    async with web_app.state.session_factory() as db:
        root = (await db.execute(select(User).where(User.name == "root"))).scalar_one()
        totp.store_secret(root, DEV_TOTP_SECRET, web_app.state.crypto)
        root.totp_enabled = True
        await db.commit()
    asked = await post_login(web_client, "/login", credentials("root"), htmx=True)
    assert asked.status_code == 200 and 'action="/login/totp"' in asked.text
    done = await post_login(
        web_client, "/login/totp", {"code": pyotp.TOTP(DEV_TOTP_SECRET).now()}, htmx=True
    )
    assert (done.status_code, done.headers["hx-redirect"]) == (200, "/")
    assert (await web_client.get("/projects")).status_code == 200


async def test_the_code_step_refuses_a_non_superadmin_like_a_wrong_code(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    """Single sign-on switched on between the password and the code ends the sign in."""
    asked = await post_login(web_client, "/login", credentials("totp_user"), htmx=True)
    assert 'action="/login/totp"' in asked.text
    await world.configure()
    code = pyotp.TOTP(DEV_TOTP_SECRET).now()
    refused = await post_login(web_client, "/login/totp", {"code": code}, htmx=True)
    wrong_one = await post_login(web_client, "/login/totp", {"code": "000000"}, htmx=True)
    assert refused.status_code == wrong_one.status_code == 401
    assert "The code is incorrect or expired." in refused.text
    assert SESSION_COOKIE_NAME not in web_client.cookies
    assert PENDING_LOGIN_COOKIE_NAME in web_client.cookies
    assert (await _reasons(web_app))[0] == "password_sign_in_off"


async def test_every_account_signs_in_with_its_password_while_sign_in_is_off(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure(enabled=False)
    response = await post_login(web_client, "/login", credentials("viewer"))
    assert (response.status_code, response.headers["location"]) == (303, "/")


async def test_the_service_refuses_a_non_superadmin_only_when_asked_to(
    db: AsyncSession, crypto: CryptoService
) -> None:
    """The service flag is what the route passes from the settings: off by default."""
    policy = AuthPolicy()
    moment = utc_now()
    email = seed_email("admin")
    refused = await authenticate(
        db,
        crypto,
        policy,
        identifier=email,
        password=DEV_PASSWORD,
        superadmins_only=True,
        now=moment,
    )
    allowed = await authenticate(
        db, crypto, policy, identifier=email, password=DEV_PASSWORD, now=moment
    )
    assert refused.status is LoginStatus.INVALID and refused.token is None
    assert allowed.status is LoginStatus.SUCCESS and allowed.token is not None
