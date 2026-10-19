"""The SMTP tab: the mail server settings, saved with their password, and the test mail.

Superadmin only: every route holds the settings capability, so any other group gets 403. The
password is never put back into the page. A save answers the form with its toast and sends the
banner area again, because it can set or clear the SMTP not configured banner. The test mail goes
through the stored settings and answers 200 with the result either way, so htmx swaps it.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoService
from app.core.display_time import display_of
from app.core.templating import render
from app.models.types import utc_now
from app.services.app_settings import (
    SMTP_PASSWORD_KEY,
    StoredSettings,
    has_secret,
    has_secret_in,
)
from app.services.mailer import TLS_MODES, MailTransport
from app.services.shell_banners import load_shell_banners
from app.services.smtp_settings import (
    SmtpForm,
    SmtpFormError,
    save_smtp,
    send_test_mail,
    validate_smtp,
)
from app.web.dependencies import (
    audit_actor,
    get_crypto,
    get_db,
    get_mail_transport,
    get_stored_settings,
)
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_smtp.html"
SAVED_FRAGMENT = "fragments/smtp_form_saved.html"
TEST_FRAGMENT = "fragments/smtp_test_result.html"

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]
Transport = Annotated[MailTransport, Depends(get_mail_transport)]


def _form_context(
    context: ManageSettings,
    form: SmtpForm,
    *,
    password_stored: bool,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the form needs: values, refusals, the choices, the toast and the stored password.

    ``password_stored`` is read by the caller: from the request's settings when nothing was
    written, from the database after a save.
    """
    return {
        "form": form,
        "errors": errors or {},
        "saved": saved,
        "tls_options": list(TLS_MODES.items()),
        "password_stored": password_stored,
        "test_to": context.user.email,
    }


@router.get("/settings/smtp", include_in_schema=False)
async def smtp_page(request: Request, context: ManageSettings, stored: Stored) -> Response:
    """The SMTP tab."""
    form = SmtpForm.from_settings(stored.smtp)
    page = {
        **tab_context("smtp"),
        **_form_context(context, form, password_stored=has_secret_in(stored, SMTP_PASSWORD_KEY)),
    }
    return render(request, "pages/settings_smtp.html", page)


@router.post("/settings/smtp", include_in_schema=False)
async def save_smtp_route(
    request: Request,
    db: Database,
    crypto: Crypto,
    context: ManageSettings,
    stored: Stored,
    host: Annotated[str, Form()] = "",
    port: Annotated[str, Form()] = "",
    tls_mode: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    from_address: Annotated[str, Form()] = "",
    remove_stored: Annotated[str, Form(alias="remove_password")] = "",
) -> Response:
    """Save the SMTP settings. A refused value answers the form again with the reasons (422)."""
    removing = remove_stored == "on"
    form = SmtpForm(host, port, tls_mode, username, password, from_address, removing)
    try:
        clean = validate_smtp(form)
    except SmtpFormError as error:
        shown = SmtpForm(host, port, tls_mode, username, "", from_address, removing)
        page = _form_context(
            context,
            shown,
            password_stored=has_secret_in(stored, SMTP_PASSWORD_KEY),
            errors=error.errors,
        )
        return render(request, FORM_FRAGMENT, page, status_code=422)
    changed = await save_smtp(db, crypto, clean, actor=audit_actor(request, context))
    if changed:
        saved = {
            "tone": "success",
            "title": "SMTP settings saved",
            "message": "They apply to the next mail.",
        }
    else:
        saved = {
            "tone": "info",
            "title": "Nothing to save",
            "message": "The settings are unchanged.",
        }
    request.state.banners = await load_shell_banners(db, display_of(request), clean.settings)
    page = _form_context(
        context,
        SmtpForm.from_settings(clean.settings),
        password_stored=await has_secret(db, SMTP_PASSWORD_KEY),
        saved=saved,
    )
    return render(request, SAVED_FRAGMENT, page)


@router.post("/settings/smtp/test", include_in_schema=False)
async def send_test_route(
    request: Request,
    db: Database,
    crypto: Crypto,
    context: ManageSettings,
    transport: Transport,
    test_to: Annotated[str, Form()] = "",
) -> Response:
    """Send a test mail through the stored settings and answer how it went."""
    check = await send_test_mail(
        db,
        crypto,
        transport,
        to=test_to,
        actor=audit_actor(request, context),
        base_url=get_settings().base_url,
        now=utc_now(),
    )
    return render(request, TEST_FRAGMENT, {"check": check})
