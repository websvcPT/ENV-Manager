"""The Allowed paths tab: the list with its status, the add dialog with live checks, remove.

Superadmin only. The list, the checks and the changes read and write the host filesystem state of
the paths, so every route holds the settings capability. A change answers 204 with events that
refresh the table and close the dialog; a refused add swaps the dialog again with the reason.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.display_time import display_of
from app.core.templating import render
from app.services.allowed_paths import (
    AllowedPathError,
    add_allowed_path,
    check_entry,
    get_entry,
    list_allowed_paths,
    remove_allowed_path,
    stored_paths,
    uncovered_deployments,
)
from app.services.settings_events import SettingsNotifier
from app.services.shell_banners import load_shell_banners
from app.web.dependencies import audit_actor, get_db, get_settings_notifier
from app.web.htmx import events_response, hx_events
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

REFRESHED_FRAGMENT = "fragments/settings_paths_refreshed.html"
ADD_FRAGMENT = "fragments/settings_path_add.html"
CHECKS_FRAGMENT = "fragments/settings_path_checks.html"
REMOVE_FRAGMENT = "fragments/settings_path_remove.html"

Database = Annotated[AsyncSession, Depends(get_db)]
Notifier = Annotated[SettingsNotifier, Depends(get_settings_notifier)]


async def _table_context(db: AsyncSession) -> dict[str, Any]:
    """What the table needs: one row per entry with its status."""
    return {"rows": await list_allowed_paths(db)}


async def _existing(db: AsyncSession) -> list[str]:
    """The stored entries, for the duplicate check."""
    return [entry.path for entry in await stored_paths(db)]


@router.get("/settings/paths", include_in_schema=False)
async def paths_page(request: Request, db: Database, _context: ManageSettings) -> Response:
    """The Allowed paths tab."""
    page = {**tab_context("paths"), **await _table_context(db)}
    return render(request, "pages/settings_paths.html", page)


@router.get("/settings/paths/table", include_in_schema=False)
async def paths_table(request: Request, db: Database, _context: ManageSettings) -> Response:
    """The table fragment, refreshed after an entry is added or removed, with the banners."""
    request.state.banners = await load_shell_banners(db, display_of(request))
    return render(request, REFRESHED_FRAGMENT, await _table_context(db))


@router.get("/settings/paths/new", include_in_schema=False)
async def add_dialog(request: Request, _context: ManageSettings) -> Response:
    """The content of the add dialog: an empty field and the neutral hint."""
    check = check_entry("", data_path=get_settings().data_path)
    return render(request, ADD_FRAGMENT, {"value": "", "check": check})


@router.get("/settings/paths/validate", include_in_schema=False)
async def validate_path(
    request: Request, db: Database, _context: ManageSettings, path: str = ""
) -> Response:
    """The check lines for a typed path, swapped into the field's message list.

    The Add button follows the result: it is disabled until the path passes every check.
    """
    check = check_entry(path, data_path=get_settings().data_path, existing=await _existing(db))
    return render(request, CHECKS_FRAGMENT, {"check": check, "with_button": True})


@router.post("/settings/paths", include_in_schema=False)
async def add_path(
    request: Request,
    db: Database,
    context: ManageSettings,
    notifier: Notifier,
    path: Annotated[str, Form()] = "",
) -> Response:
    """Add an allowed path. A refused path swaps the dialog with the reason (422)."""
    try:
        await add_allowed_path(
            db,
            path,
            actor=audit_actor(request, context),
            data_path=get_settings().data_path,
            notifier=notifier,
        )
    except AllowedPathError as error:
        check = check_entry(path, data_path=get_settings().data_path, existing=await _existing(db))
        page = {"value": path, "check": check, "error": str(error)}
        return render(request, ADD_FRAGMENT, page, status_code=422)
    return events_response("paths-changed", "close-dialogs")


async def _remove_view(db: AsyncSession, path_id: int) -> dict[str, Any] | None:
    """The confirm dialog of an entry: its path and how many deployments it would leave outside."""
    entry = await get_entry(db, path_id)
    if entry is None:
        return None
    flagged = await uncovered_deployments(db, entry, await stored_paths(db))
    return {"path_id": entry.id, "path": entry.path, "flagged": flagged, "gone": False}


@router.get("/settings/paths/{path_id:int}/remove", include_in_schema=False)
async def remove_dialog(
    request: Request, path_id: int, db: Database, _context: ManageSettings
) -> Response:
    """The confirm dialog of removing an entry."""
    view = await _remove_view(db, path_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "path not found")
    return render(request, REMOVE_FRAGMENT, view)


@router.post("/settings/paths/{path_id:int}/remove", include_in_schema=False)
async def remove_path(
    request: Request, path_id: int, db: Database, context: ManageSettings, notifier: Notifier
) -> Response:
    """Remove an entry. Nothing on disk is touched.

    An entry that was removed meanwhile answers 409 with the dialog saying so, and the table
    refreshes to show the list as it is.
    """
    try:
        await remove_allowed_path(
            db, path_id, actor=audit_actor(request, context), notifier=notifier
        )
    except AllowedPathError:
        view = {"path_id": path_id, "path": "", "flagged": 0, "gone": True}
        response = render(request, REMOVE_FRAGMENT, view, status_code=409)
        response.headers["HX-Trigger"] = hx_events("paths-changed")
        return response
    return events_response("paths-changed", "close-dialogs")
