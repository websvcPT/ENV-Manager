"""The Account and Users pages for accounts linked to single sign-on.

While sign in through the provider is on, a linked account that is not a superadmin gets its
profile, password and second factor from the provider: the Account page shows them read only and
the routes that would change them refuse, and the Users page hides the second factor reset.
"""

import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.passwords import verify_password
from app.models import User, UserGroup
from app.seeds import DEV_PASSWORD
from app.services import totp
from app.tests.conftest import sign_in
from app.tests.sso_helpers import BASE_URL, ISSUER, World
from app.tests.sso_web_helpers import link, sso_world
from app.tests.web_helpers import HTMX, main_of, post

NOTE_MANAGED = (
    "You sign in with SSO. Your name, email, password and second factor are managed in your "
    "SSO account and updated here at each sign in."
)
NOTE_SUPERADMIN = (
    "You can also sign in with SSO. Your name and email follow your SSO account and are "
    "updated here at each sign in."
)


@pytest.fixture(autouse=True)
def base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address the application is reached at, before the application starts."""
    monkeypatch.setenv("ENVMANAGER_BASE_URL", BASE_URL)


@pytest.fixture
def world(web_app: FastAPI) -> World:
    """The application's side of a sign in against a fresh fake provider."""
    return sso_world(web_app)


async def enable_totp_for(web_app: FastAPI, name: str) -> None:
    """Turn the second factor on for a user directly."""
    async with web_app.state.session_factory() as db:
        user = (await db.execute(select(User).where(User.name == name))).scalar_one()
        totp.activate(user, totp.generate_secret(), 1, web_app.state.crypto)
        await db.commit()


async def stored(web_app: FastAPI, name: str) -> User:
    """The user as stored now."""
    async with web_app.state.session_factory() as db:
        user: User = (await db.execute(select(User).where(User.name == name))).scalar_one()
        return user


async def account(http: httpx.AsyncClient) -> str:
    """The body of the Account page."""
    response = await http.get("/account")
    assert response.status_code == 200
    return main_of(response.text)


async def sign_in_then_configure(
    http: httpx.AsyncClient, web_app: FastAPI, world: World, name: str, **settings: object
) -> None:
    """Sign in with the password (while sign in through the provider is off), then switch it on.

    The session stays valid, so the pages can be read as that user while the provider is on.
    """
    await sign_in(http, name)
    await link(web_app, name)
    await world.configure(**settings)


async def test_a_linked_admin_sees_the_provider_owning_the_account(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "admin")
    await enable_totp_for(web_app, "admin")
    await link(web_app, "admin")
    await world.configure()
    page = await account(web_client)
    assert NOTE_MANAGED in page
    button = re.search(r'<a href="([^"]+)"[^>]*data-sso-manage[^>]*>', page)
    assert button is not None
    assert button.group(1) == f"{ISSUER}/account"
    assert 'target="_blank"' in button.group(0)
    assert 'rel="noopener noreferrer"' in button.group(0)
    assert "Manage account in SSO" in page
    profile = page.split('data-fragment="account-profile"', 1)[1].split("</section>", 1)[0]
    assert "From SSO" in profile and 'data-profile="name"' in profile
    assert "admin@example.test" in profile and "<input" not in profile
    assert "Not used with SSO" in page
    assert 'id="account-password-form"' not in page and 'name="new_password"' not in page
    panel = page.split('data-fragment="account-totp"', 1)[1].split("</section>", 1)[0]
    assert "Skipped with SSO" in panel and "Enabled" in panel
    assert re.search(r"<button[^>]*\sdisabled[^>]*>Remove two-factor</button>", panel)
    assert 'data-fragment="account-sessions"' in page


async def test_a_linked_admin_without_two_factor_has_the_setup_button_disabled(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in_then_configure(web_client, web_app, world, "viewer")
    panel = (await account(web_client)).split('data-fragment="account-totp"', 1)[1]
    panel = panel.split("</section>", 1)[0]
    assert "Skipped with SSO" in panel and "Not enabled" in panel
    assert re.search(r"<button[^>]*\sdisabled[^>]*>Set up two-factor</button>", panel)


async def test_the_account_url_of_the_settings_is_the_button_address(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in_then_configure(
        web_client, web_app, world, "admin", account_url="https://idp.example.test/me"
    )
    page = await account(web_client)
    assert 'href="https://idp.example.test/me"' in page


async def test_a_linked_superadmin_keeps_everything_and_gets_the_note_and_button(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in_then_configure(web_client, web_app, world, "root")
    page = await account(web_client)
    assert NOTE_SUPERADMIN in page and NOTE_MANAGED not in page
    assert "Manage account in SSO" in page and f'href="{ISSUER}/account"' in page
    assert 'id="account-password-form"' in page
    assert "Not used with SSO" not in page and "Skipped with SSO" not in page
    assert 'data-fragment="account-profile"' not in page
    assert not re.search(r"<button[^>]*\sdisabled[^>]*>Set up two-factor</button>", page)


async def test_an_account_that_is_not_linked_is_unchanged(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "admin")
    await world.configure()
    page = await account(web_client)
    assert "SSO" not in page
    assert 'id="account-password-form"' in page
    assert not re.search(r"<button[^>]*\sdisabled[^>]*>Set up two-factor</button>", page)


async def test_a_linked_account_is_unchanged_while_sign_in_is_off(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "admin")
    await link(web_app, "admin")
    await world.configure(enabled=False)
    page = await account(web_client)
    assert "SSO" not in page and 'id="account-password-form"' in page


async def test_the_routes_that_change_a_managed_account_refuse(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in_then_configure(web_client, web_app, world, "admin")
    await enable_totp_for(web_app, "admin")
    before = await stored(web_app, "admin")
    new = "a brand new passphrase"
    refused = [
        await post(
            web_client,
            "/account/password",
            {"current_password": DEV_PASSWORD, "new_password": new, "confirm_password": new},
        ),
        await post(web_client, "/account/totp/setup"),
        await post(web_client, "/account/totp/enable", {"current_password": DEV_PASSWORD}),
        await post(web_client, "/account/totp/remove", {"current_password": DEV_PASSWORD}),
        await web_client.get("/account/totp/remove", headers=HTMX),
    ]
    assert [response.status_code for response in refused] == [403] * 5
    after = await stored(web_app, "admin")
    assert after.password_hash == before.password_hash and after.totp_enabled
    assert verify_password(DEV_PASSWORD, after.password_hash)[0]


async def test_the_refused_routes_work_for_an_unlinked_account_and_a_superadmin(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "root")
    await link(web_app, "root")
    await world.configure()
    setup = await post(web_client, "/account/totp/setup")
    assert setup.status_code == 200 and 'id="totp-qr"' in setup.text
    other = await web_client.get("/account/totp/remove", headers=HTMX)
    assert other.status_code == 200
    wrong = await post(
        web_client,
        "/account/password",
        {"current_password": "wrong", "new_password": "x", "confirm_password": "x"},
    )
    assert wrong.status_code == 422


async def test_the_two_factor_panel_refresh_stays_read_only_for_a_managed_account(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in_then_configure(web_client, web_app, world, "viewer")
    panel = await web_client.get("/account/totp", headers=HTMX)
    assert panel.status_code == 200 and "Skipped with SSO" in panel.text


def _list_row(page: str, user_id: int) -> str:
    return page.split(f'id="user-{user_id}"', 1)[1].split("</tr>", 1)[0]


async def test_the_users_list_and_detail_mark_linked_accounts(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "root")
    linked = await link(web_app, "admin")
    other = await stored(web_app, "viewer")
    await world.configure()
    page = (await web_client.get("/users")).text
    assert 'data-status="sso"' in _list_row(page, linked.id)
    assert 'data-status="sso"' not in _list_row(page, other.id)
    detail = main_of((await web_client.get(f"/users/{linked.id}")).text)
    assert 'data-status="sso"' in detail
    assert 'data-status="sso"' not in main_of((await web_client.get(f"/users/{other.id}")).text)


async def test_the_badge_stays_while_sign_in_is_off(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "root")
    linked = await link(web_app, "admin")
    page = (await web_client.get("/users")).text
    assert 'data-status="sso"' in _list_row(page, linked.id)


async def test_the_reset_is_absent_and_refused_for_a_managed_account(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "root")
    target = await link(web_app, "totp_user")
    await world.configure()
    detail = main_of((await web_client.get(f"/users/{target.id}")).text)
    assert "Reset two-factor" not in detail and "Two-factor on" in detail
    dialog = await web_client.get(f"/users/{target.id}/confirm/reset-2fa", headers=HTMX)
    reset = await post(web_client, f"/users/{target.id}/reset-2fa", {"from": "detail"})
    assert dialog.status_code == reset.status_code == 403
    assert (await stored(web_app, "totp_user")).totp_enabled


async def test_the_reset_stays_for_accounts_the_provider_does_not_manage(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "root")
    unlinked = await stored(web_app, "totp_user")
    await world.configure()
    detail = main_of((await web_client.get(f"/users/{unlinked.id}")).text)
    assert "Reset two-factor" in detail
    await link(web_app, "totp_user")
    await world.configure(enabled=False)
    again = main_of((await web_client.get(f"/users/{unlinked.id}")).text)
    assert "Reset two-factor" in again
    reset = await post(web_client, f"/users/{unlinked.id}/reset-2fa", {"from": "detail"})
    assert reset.status_code == 200
    assert not (await stored(web_app, "totp_user")).totp_enabled


async def test_a_linked_superadmin_keeps_the_reset_of_its_second_factor(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    """A superadmin is not managed by the provider, so another superadmin may reset it."""
    await sign_in(web_client, "root")
    async with web_app.state.session_factory() as db:
        other = (await db.execute(select(User).where(User.name == "admin"))).scalar_one()
        other.group = UserGroup.SUPERADMIN
        await db.commit()
    await enable_totp_for(web_app, "admin")
    await link(web_app, "admin")
    await world.configure()
    detail = main_of((await web_client.get(f"/users/{other.id}")).text)
    assert "Reset two-factor" in detail


async def test_a_linked_account_is_unchanged_while_sign_in_has_no_stored_secret(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await sign_in(web_client, "admin")
    await link(web_app, "admin")
    await world.configure(client_secret="")
    page = await account(web_client)
    assert "SSO" not in page and 'id="account-password-form"' in page
