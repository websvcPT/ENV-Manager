"""The Notifications tab: which events send mail, to whom for the events about a project, and the
outbound webhook for deploy results.

Superadmin only: every route holds the settings capability, so any other group gets 403. The
matrix and the webhook are saved as a whole, validated on the server and effective for the next
event, because the notifier reads the stored settings whenever it runs. The shared secret is never
put back into the page. A save answers the form with its toast and sends the banner area again,
because it can clear the webhook banner. The test event goes through the stored settings and
answers 200 with the result either way, so htmx swaps it.
"""

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.display_time import display_of
from app.core.templating import render
from app.models.types import utc_now
from app.services.app_settings import WEBHOOK_SECRET_KEY, StoredSettings, has_secret
from app.services.audit import AuditActor
from app.services.notification_settings import (
    NotificationsForm,
    NotificationsFormError,
    recipient_choices,
    save_notifications,
    validate_notifications,
)
from app.services.notify_catalog import EVENT_ROWS
from app.services.shell_banners import load_shell_banners
from app.services.webhook import WebhookTransport
from app.services.webhook_delivery import send_test_event
from app.web.dependencies import get_crypto, get_db, get_stored_settings
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_notifications.html"
SAVED_FRAGMENT = "fragments/notifications_saved.html"
TEST_FRAGMENT = "fragments/webhook_test_result.html"

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]


def _actor(request: Request, context: ManageSettings) -> AuditActor:
    """The signed-in superadmin as the actor of an audit entry."""
    return AuditActor.for_user(context.user, client_address(request))


def _transport(request: Request) -> WebhookTransport:
    """The application's webhook transport."""
    transport: WebhookTransport = request.app.state.webhook_transport
    return transport


async def _form_context(
    db: AsyncSession,
    form: NotificationsForm,
    *,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the tab needs: the rows with their state, the webhook fields, the choices, refusals,
    the toast and whether a webhook secret is stored."""
    return {
        "rows": [
            {
                "row": row,
                "enabled": row.kind in form.enabled,
                "recipients": form.recipients.get(row.kind, ""),
            }
            for row in EVENT_ROWS
        ],
        "form": form,
        "recipient_choices": recipient_choices(),
        "errors": errors or {},
        "saved": saved,
        "secret_stored": await has_secret(db, WEBHOOK_SECRET_KEY),
    }


@router.get("/settings/notifications", include_in_schema=False)
async def notifications_page(
    request: Request, db: Database, _context: ManageSettings, stored: Stored
) -> Response:
    """The Notifications tab."""
    form = NotificationsForm.from_settings(stored.notifications)
    page = {**tab_context("notifications"), **await _form_context(db, form)}
    return render(request, "pages/settings_notifications.html", page)


@router.post("/settings/notifications", include_in_schema=False)
async def save_notifications_route(
    request: Request, db: Database, crypto: Crypto, context: ManageSettings, stored: Stored
) -> Response:
    """Save the tab. A refused value answers the form again with the reasons (422); the typed
    secret is not put back."""
    submitted = {key: str(value) for key, value in (await request.form()).items()}
    form = NotificationsForm.from_submission(submitted)
    secret_stored = await has_secret(db, WEBHOOK_SECRET_KEY)
    try:
        clean = validate_notifications(
            form, stored=stored.notifications, secret_stored=secret_stored
        )
    except NotificationsFormError as error:
        shown = NotificationsForm(
            form.enabled,
            form.recipients,
            form.webhook_enabled,
            form.webhook_url,
            remove_webhook_secret=form.remove_webhook_secret,
        )
        page = await _form_context(db, shown, errors=error.errors)
        return render(request, FORM_FRAGMENT, page, status_code=422)
    changed = await save_notifications(db, crypto, clean, actor=_actor(request, context))
    if changed:
        saved = {
            "tone": "success",
            "title": "Notification settings saved",
            "message": "They apply to the next event.",
        }
    else:
        saved = {
            "tone": "info",
            "title": "Nothing to save",
            "message": "The settings are unchanged.",
        }
    request.state.banners = await load_shell_banners(db, display_of(request), stored.smtp)
    shown = NotificationsForm.from_settings(clean.settings)
    return render(request, SAVED_FRAGMENT, await _form_context(db, shown, saved=saved))


@router.post("/settings/notifications/webhook/test", include_in_schema=False)
async def send_test_event_route(
    request: Request, db: Database, crypto: Crypto, context: ManageSettings, stored: Stored
) -> Response:
    """Post the test event through the stored settings and answer how it went."""
    check = await send_test_event(
        db,
        crypto,
        _transport(request),
        actor=_actor(request, context),
        now=utc_now(),
        unix_time=int(time.time()),
    )
    request.state.banners = await load_shell_banners(db, display_of(request), stored.smtp)
    return render(request, TEST_FRAGMENT, {"check": check})
