"""The web application announces its events to one notifier and never waits for mail.

Each test puts a real notifier on the running application, with the mail transport recorded, and
drives the real routes: what the browser does is what produces the mail.
"""

import asyncio
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from starlette.requests import Request

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import User
from app.seeds import DEV_PASSWORD
from app.services.notifier import Notifications
from app.services.notify_events import LockoutScope, LockoutStarted
from app.tests.conftest import sign_in
from app.tests.notify_helpers import (
    RecordingEvents,
    RecordingTransport,
    build_notifier,
    configure_smtp,
    decoded_text,
    mime_of,
)
from app.tests.web_helpers import post
from app.web.dependencies import get_deploy_engine

TYPED = "typed-into-the-name-field"


@dataclass
class Mailbox:
    """The notifier on the application and the transport it sends through."""

    notifier: Notifications
    transport: RecordingTransport

    async def subjects(self) -> list[str]:
        """Wait for every event so far and list the subjects of the mails, oldest first."""
        await self.notifier.idle()
        return [sent.mail.subject for sent in self.transport.sent]

    async def texts(self) -> list[str]:
        """The decoded text of every mail sent so far."""
        await self.notifier.idle()
        return [decoded_text(await mime_of(sent.mail)) for sent in self.transport.sent]


@pytest.fixture
async def mailbox(web_app: FastAPI) -> Mailbox:
    """SMTP configured on the application's database and a recording notifier in its place."""
    transport = RecordingTransport()
    await configure_smtp(web_app.state.engine, web_app.state.crypto)
    notifier = build_notifier(web_app.state.engine, web_app.state.crypto, transport)
    web_app.state.notifier = notifier
    return Mailbox(notifier, transport)


async def user_id(web_app: FastAPI, name: str) -> int:
    """The id of a user by name."""
    async with web_app.state.session_factory() as db:
        return int((await db.execute(select(User.id).where(User.name == name))).scalar_one())


async def test_the_application_gives_every_seam_the_same_notifier(web_app: FastAPI) -> None:
    """Settings, backups, deploys and the events share one notifier object."""
    state = web_app.state
    assert isinstance(state.notifier, Notifications)
    assert state.settings_notifier is state.notifier and state.backup_notifier is state.notifier
    request = Request({"type": "http", "app": web_app, "headers": []})
    assert get_deploy_engine(request).notifier is state.notifier


async def test_disabling_a_user_in_the_browser_mails_the_superadmin(
    web_client: httpx.AsyncClient, web_app: FastAPI, mailbox: Mailbox
) -> None:
    """The route passes the notifier: one mail to the superadmin, naming who did it."""
    await sign_in(web_client, "root")
    response = await post(web_client, f"/users/{await user_id(web_app, 'viewer')}/disable")
    assert response.status_code == 204
    assert await mailbox.subjects() == ["[ENV Manager] User disabled: viewer"]
    assert mailbox.transport.to() == ["root@example.test"]
    assert "Done by: root" in (await mailbox.texts())[0]


async def test_creating_a_user_in_the_browser_mails_without_the_one_time_password(
    web_client: httpx.AsyncClient, mailbox: Mailbox
) -> None:
    """The password is on the screen once and appears in no mail."""
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        "/users",
        {"name": "erin", "email": "erin@example.test", "group": "admin"},
    )
    assert response.status_code == 200
    assert await mailbox.subjects() == ["[ENV Manager] User created: erin"]
    assert "Group: admin" in (await mailbox.texts())[0]
    shown = response.text.split("hx-ignore", 1)[1]
    candidates = {
        part for part in shown.replace("<", " ").replace(">", " ").split() if len(part) == 16
    }
    assert candidates
    texts = await mailbox.texts()
    assert not any(secret in text for secret in candidates for text in texts)


async def test_resetting_two_factor_in_the_browser_mails_the_superadmin(
    web_client: httpx.AsyncClient, web_app: FastAPI, mailbox: Mailbox
) -> None:
    """The superadmin reset is announced."""
    await sign_in(web_client, "root")
    response = await post(web_client, f"/users/{await user_id(web_app, 'totp_user')}/reset-2fa")
    assert response.status_code == 204
    assert await mailbox.subjects() == ["[ENV Manager] Two-factor reset: totp_user"]


async def test_changing_your_own_password_in_the_browser_mails_the_superadmin(
    web_client: httpx.AsyncClient, mailbox: Mailbox
) -> None:
    """The account page announces a password change and the mail has no password."""
    await sign_in(web_client, "admin")
    new = "a-new-password-for-the-admin"
    response = await post(
        web_client,
        "/account/password",
        {"current_password": DEV_PASSWORD, "new_password": new, "confirm_password": new},
    )
    assert response.status_code == 200
    assert await mailbox.subjects() == ["[ENV Manager] Password changed: admin"]
    texts = await mailbox.texts()
    assert not any(secret in text for secret in (new, DEV_PASSWORD) for text in texts)


async def test_failed_sign_ins_that_lock_a_name_mail_the_superadmin_without_the_typed_text(
    web_client: httpx.AsyncClient, mailbox: Mailbox
) -> None:
    """A password typed into the name field is counted, and the lock is announced without it."""
    await web_client.get("/healthz")
    for _ in range(5):
        await web_client.post(
            "/login",
            data={
                "identifier": TYPED,
                "password": "x",
                "csrf_token": web_client.cookies[CSRF_COOKIE_NAME],
            },
        )
    assert await mailbox.subjects() == ["[ENV Manager] Lockout after repeated failed attempts"]
    assert mailbox.transport.to() == ["root@example.test"]
    assert not any(TYPED in text for text in await mailbox.texts())


async def test_a_sign_in_from_a_new_address_mails_the_superadmin(
    web_app: FastAPI, mailbox: Mailbox
) -> None:
    """The first address is learned, a second one is announced with the address."""
    for address in ("198.51.100.4", "198.51.100.4", "203.0.113.50"):
        transport = httpx.ASGITransport(app=web_app, client=(address, 4000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"Accept": "text/html"}
        ) as browser:
            await sign_in(browser, "viewer")
    assert await mailbox.subjects() == ["[ENV Manager] New sign-in address for viewer"]
    assert "Address: 203.0.113.50" in (await mailbox.texts())[0]


async def test_bad_api_tokens_that_lock_an_address_are_announced_by_the_api_dependency(
    web_app: FastAPI, web_client: httpx.AsyncClient
) -> None:
    """The bearer dependency hands its notifier to the authentication."""
    recorder = RecordingEvents()
    web_app.state.notifier = recorder
    for _ in range(10):
        await web_client.post(
            "/api/v1/services/1/deploy", headers={"Authorization": "Bearer envm_" + "0" * 48}
        )
    assert recorder.events == [LockoutStarted(LockoutScope.API_ADDRESS, None, "127.0.0.1", 15)]


async def test_a_mail_server_that_never_answers_does_not_hold_up_the_request(
    web_client: httpx.AsyncClient, web_app: FastAPI, mailbox: Mailbox
) -> None:
    """The request that causes the event finishes at once; the mail is sent when the server
    answers, and a failing server never fails the action."""
    mailbox.transport.hang = asyncio.Event()
    await sign_in(web_client, "root")
    response = await asyncio.wait_for(
        post(web_client, f"/users/{await user_id(web_app, 'viewer')}/disable"), timeout=5
    )
    assert response.status_code == 204
    assert mailbox.transport.sent == []
    async with web_app.state.session_factory() as db:
        viewer = (await db.execute(select(User).where(User.name == "viewer"))).scalar_one()
        assert viewer.is_active is False
    mailbox.transport.hang.set()
    assert await mailbox.subjects() == ["[ENV Manager] User disabled: viewer"]


async def test_a_failing_mail_server_does_not_fail_the_action_and_is_counted(
    web_client: httpx.AsyncClient, web_app: FastAPI, mailbox: Mailbox
) -> None:
    """The user is disabled whatever the server does, and the banner is told after the retries."""
    from app.tests.notify_helpers import permanent_failure

    mailbox.transport.failures = [permanent_failure()]
    await sign_in(web_client, "root")
    response = await post(web_client, f"/users/{await user_id(web_app, 'viewer')}/disable")
    assert response.status_code == 204
    await mailbox.notifier.idle()
    page = (await web_client.get("/settings/security")).text
    assert "Notification emails are failing: 1 mail could not be sent" in page
    assert "The mail server refused the username or password." in page
