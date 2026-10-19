"""The Notifications tab: which events send mail, and to whom for the events about a project.

Superadmin only: every route holds the settings capability, so any other group gets 403. The
matrix is saved as a whole, validated on the server and effective for the next event, because the
notifier reads the stored switches whenever it runs.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.services.app_settings import StoredSettings
from app.services.audit import AuditActor
from app.services.notification_settings import (
    NotificationsForm,
    NotificationsFormError,
    recipient_choices,
    save_notifications,
    validate_notifications,
)
from app.services.notify_catalog import EVENT_ROWS
from app.web.dependencies import get_db, get_stored_settings
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_notifications.html"

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]


def _actor(request: Request, context: ManageSettings) -> AuditActor:
    """The signed-in superadmin as the actor of an audit entry."""
    return AuditActor.for_user(context.user, client_address(request))


def _form_context(
    form: NotificationsForm,
    *,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the matrix needs: the rows with their state, the choices, refusals and the toast."""
    return {
        "rows": [
            {
                "row": row,
                "enabled": row.kind in form.enabled,
                "recipients": form.recipients.get(row.kind, ""),
            }
            for row in EVENT_ROWS
        ],
        "recipient_choices": recipient_choices(),
        "errors": errors or {},
        "saved": saved,
    }


@router.get("/settings/notifications", include_in_schema=False)
async def notifications_page(
    request: Request, _context: ManageSettings, stored: Stored
) -> Response:
    """The Notifications tab."""
    form = NotificationsForm.from_settings(stored.notifications)
    page = {**tab_context("notifications"), **_form_context(form)}
    return render(request, "pages/settings_notifications.html", page)


@router.post("/settings/notifications", include_in_schema=False)
async def save_notifications_route(
    request: Request, db: Database, context: ManageSettings, stored: Stored
) -> Response:
    """Save the matrix. A refused choice answers the form again with the reasons (422)."""
    submitted = {key: str(value) for key, value in (await request.form()).items()}
    form = NotificationsForm.from_submission(submitted)
    try:
        clean = validate_notifications(form, stored=stored.notifications)
    except NotificationsFormError as error:
        page = _form_context(form, errors=error.errors)
        return render(request, FORM_FRAGMENT, page, status_code=422)
    changes = await save_notifications(db, clean, actor=_actor(request, context))
    if changes:
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
    shown = NotificationsForm.from_settings(clean)
    return render(request, FORM_FRAGMENT, _form_context(shown, saved=saved))
