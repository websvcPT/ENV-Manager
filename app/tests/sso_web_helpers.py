"""Helpers for the single sign-on tests that go through the web application.

The application under test gets the fake provider's transport, and the base URL its redirect
address is checked against (set by the test module's fixture before the application starts).
"""

import html
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import AuditEntry, User, UserIdentity
from app.services.sso_provider import SsoProvider
from app.tests.sso_helpers import CODE, ISSUER, FakeIdp, World, good_claims


def sso_world(web_app: FastAPI) -> World:
    """The seeded application's side of a sign in, against a fresh fake provider."""
    idp = FakeIdp()
    provider = SsoProvider(idp.transport())
    web_app.state.sso_provider = provider
    return World(web_app.state.engine, web_app.state.crypto, idp, provider)


def visible(page: str) -> str:
    """The page text with its entities decoded, so a sentence with an apostrophe can be found."""
    return html.unescape(page)


async def begin(http: httpx.AsyncClient, world: World) -> dict[str, list[str]]:
    """Follow the sign in link and return the query of the redirect to the provider."""
    response = await http.get("/login/sso")
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    world.idp.challenge = query["code_challenge"][0]
    return query


async def come_back(
    http: httpx.AsyncClient,
    world: World,
    query: dict[str, list[str]],
    *,
    changes: dict[str, object] | None = None,
    claims: dict[str, object] | None = None,
    state: str | None = None,
    code: str | None = CODE,
    error: str | None = None,
) -> httpx.Response:
    """Return from the provider with a token for the claims (the good ones by default)."""
    issued = claims if claims is not None else good_claims(query["nonce"][0], **(changes or {}))
    world.idp.id_token = world.idp.sign(issued)
    params: dict[str, str] = {"state": query["state"][0] if state is None else state}
    if code is not None:
        params["code"] = code
    if error is not None:
        params["error"] = error
    return await http.get("/login/sso/callback", params=params)


async def sign_in_with_sso(
    http: httpx.AsyncClient, world: World, **changes: object
) -> httpx.Response:
    """A whole sign in: the link, the provider and the callback."""
    query = await begin(http, world)
    return await come_back(http, world, query, changes=changes)


async def link(web_app: FastAPI, name: str, subject: str | None = None) -> User:
    """Link the seeded user to an identity at the fake provider."""
    async with web_app.state.session_factory() as db:
        user: User = (await db.execute(select(User).where(User.name == name))).scalar_one()
        db.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=subject or f"sub-{name}"))
        await db.commit()
        return user


async def audit_entries(web_app: FastAPI, action: str) -> list[AuditEntry]:
    """The audit entries of an action, oldest first."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


async def csrf_token(http: httpx.AsyncClient) -> str:
    """The CSRF token the application issued to the client."""
    await http.get("/healthz")
    return http.cookies[CSRF_COOKIE_NAME]
