"""The Notifications tab: which events send mail, to whom for the events about a project, and the
outbound webhook for deploy results.

Superadmin only: every route holds the settings capability, so any other group gets 403. The
matrix and the webhook are saved as a whole, validated on the server and effective for the next
event, because the notifier reads the stored settings whenever it runs. The shared secret and the
webhook address are never put back into the page; the address shows as its scheme and host. A
save answers the form with its toast and sends the banner area again, because it can clear the
webhook banner. The test event goes through the stored settings and answers 200 with the result
either way, so htmx swaps it.
"""

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.display_time import display_of
from app.core.templating import render
from app.models.types import utc_now
from app.services.app_settings import (
    WEBHOOK_SECRET_KEY,
    WEBHOOK_URL_KEY,
    StoredSettings,
    has_secret,
    has_secret_in,
)
from app.services.notification_settings import (
    NotificationsForm,
    NotificationsFormError,
    recipient_choices,
    save_notifications,
    stored_webhook_origin,
    validate_notifications,
)
from app.services.notify_catalog import EVENT_ROWS
from app.services.shell_banners import load_shell_banners
from app.services.webhook import WebhookTransport
from app.services.webhook_delivery import send_test_event
from app.web.dependencies import (
    audit_actor,
    get_crypto,
    get_db,
    get_stored_settings,
    get_webhook_transport,
)
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_notifications.html"
SAVED_FRAGMENT = "fragments/notifications_saved.html"
TEST_FRAGMENT = "fragments/webhook_test_result.html"

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]
Transport = Annotated[WebhookTransport, Depends(get_webhook_transport)]


def _form_context(
    form: NotificationsForm,
    *,
    secret_stored: bool,
    url_stored: bool,
    url_origin: str,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the tab needs: the rows with their state, the webhook fields, the choices, refusals,
    the toast, whether a webhook secret and address are stored and the scheme and host of the
    stored address (empty when none is stored or it cannot be read), as the caller read them."""
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
        "secret_stored": secret_stored,
        "url_stored": url_stored,
        "url_origin": url_origin,
    }


@router.get("/settings/notifications", include_in_schema=False)
async def notifications_page(
    request: Request, db: Database, crypto: Crypto, _context: ManageSettings, stored: Stored
) -> Response:
    """The Notifications tab."""
    form = NotificationsForm.from_settings(stored.notifications)
    url_stored = has_secret_in(stored, WEBHOOK_URL_KEY)
    page = {
        **tab_context("notifications"),
        **_form_context(
            form,
            secret_stored=has_secret_in(stored, WEBHOOK_SECRET_KEY),
            url_stored=url_stored,
            url_origin=await stored_webhook_origin(db, crypto) if url_stored else "",
        ),
    }
    return render(request, "pages/settings_notifications.html", page)


@router.post("/settings/notifications", include_in_schema=False)
async def save_notifications_route(
    request: Request, db: Database, crypto: Crypto, context: ManageSettings, stored: Stored
) -> Response:
    """Save the tab. A refused value answers the form again with the reasons (422); the typed
    secret is not put back."""
    submitted = {key: str(value) for key, value in (await request.form()).items()}
    form = NotificationsForm.from_submission(submitted)
    secret_stored = has_secret_in(stored, WEBHOOK_SECRET_KEY)
    url_stored = has_secret_in(stored, WEBHOOK_URL_KEY)
    try:
        clean = validate_notifications(
            form,
            stored=stored.notifications,
            url_stored=url_stored,
            secret_stored=secret_stored,
        )
    except NotificationsFormError as error:
        shown = NotificationsForm(
            form.enabled,
            form.recipients,
            form.webhook_enabled,
            remove_webhook_url=form.remove_webhook_url,
            remove_webhook_secret=form.remove_webhook_secret,
        )
        page = _form_context(
            shown,
            secret_stored=secret_stored,
            url_stored=url_stored,
            url_origin=await stored_webhook_origin(db, crypto) if url_stored else "",
            errors=error.errors,
        )
        return render(request, FORM_FRAGMENT, page, status_code=422)
    changed = await save_notifications(db, crypto, clean, actor=audit_actor(request, context))
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
    page = _form_context(
        shown,
        secret_stored=await has_secret(db, WEBHOOK_SECRET_KEY),
        url_stored=await has_secret(db, WEBHOOK_URL_KEY),
        url_origin=await stored_webhook_origin(db, crypto),
        saved=saved,
    )
    return render(request, SAVED_FRAGMENT, page)


@router.post("/settings/notifications/webhook/test", include_in_schema=False)
async def send_test_event_route(
    request: Request,
    db: Database,
    crypto: Crypto,
    context: ManageSettings,
    stored: Stored,
    transport: Transport,
) -> Response:
    """Post the test event through the stored settings and answer how it went."""
    check = await send_test_event(
        db,
        crypto,
        transport,
        actor=audit_actor(request, context),
        now=utc_now(),
        unix_time=int(time.time()),
    )
    request.state.banners = await load_shell_banners(db, display_of(request), stored.smtp)
    return render(request, TEST_FRAGMENT, {"check": check})
