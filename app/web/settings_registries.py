"""The Registries tab: the Docker registry logins, their dialogs and the login test.

Superadmin only, like every settings tab. The list is refreshed by an event, and add, edit,
remove and test open loaded dialogs. A stored token is never put back into a page: the token
field is empty whenever it is drawn (also after a refusal), and an empty token on edit keeps the
stored one. A change answers 204 with events that refresh the table and close the dialog; a
refused one swaps the dialog again with a reason next to each field (422). A registry that was
removed meanwhile answers 404 with a dialog that says so and the event that refreshes the table.
Test login runs ``docker login`` through the Docker CLI of the app and needs a working Docker.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.templating import render
from app.models import DockerRegistry
from app.services.docker_cli import DockerCli
from app.services.registries import (
    RegistryFormError,
    RegistryNotFoundError,
    add_registry,
    list_registries,
    remove_registry,
    update_registry,
)
from app.services.registry_check import check_registry_login
from app.web.dependencies import get_crypto, get_db, get_docker_cli
from app.web.htmx import events_response, hx_events
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_registry_form.html"
REMOVE_FRAGMENT = "fragments/settings_registry_remove.html"
TEST_FRAGMENT = "fragments/settings_registry_test.html"
GONE_FRAGMENT = "fragments/settings_registry_gone.html"
TABLE_FRAGMENT = "fragments/settings_registries_table.html"

Database = Annotated[AsyncSession, Depends(get_db)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]
Docker = Annotated[DockerCli, Depends(get_docker_cli)]


@dataclass(frozen=True)
class RegistryRow:
    """What a page shows of a registry: never the sealed token."""

    id: int
    host: str
    username: str
    updated_at: datetime

    @classmethod
    def of(cls, registry: DockerRegistry) -> Self:
        """The shown fields of a stored registry."""
        return cls(registry.id, registry.host, registry.username, registry.updated_at)


async def _table_context(db: AsyncSession, cli: DockerCli) -> dict[str, Any]:
    """What the table needs: the rows and the Docker status that decides on Test login."""
    rows = [RegistryRow.of(registry) for registry in await list_registries(db)]
    return {"rows": rows, "docker": await cli.status()}


def _form_context(
    mode: str,
    *,
    registry_id: int | None = None,
    stored_host: str = "",
    host: str = "",
    username: str = "",
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the add and edit dialog needs; there is no field for the token to be filled back."""
    return {
        "mode": mode,
        "registry_id": registry_id,
        "stored_host": stored_host,
        "form": {"host": host, "username": username},
        "errors": errors or {},
    }


def _gone(request: Request, dialog_id: str) -> Response:
    """The dialog of a registry that is no longer in the list, and the event that refreshes."""
    response = render(request, GONE_FRAGMENT, {"dialog_id": dialog_id}, status_code=404)
    response.headers["HX-Trigger"] = hx_events("registries-changed")
    return response


async def _stored_row(db: AsyncSession, registry_id: int) -> RegistryRow | None:
    """The shown fields of a registry, None when it is not in the list."""
    registry = await db.get(DockerRegistry, registry_id)
    return RegistryRow.of(registry) if registry is not None else None


@router.get("/settings/registries", include_in_schema=False)
async def registries_page(
    request: Request, db: Database, cli: Docker, _context: ManageSettings
) -> Response:
    """The Registries tab."""
    page = {**tab_context("registries"), **await _table_context(db, cli)}
    return render(request, "pages/settings_registries.html", page)


@router.get("/settings/registries/table", include_in_schema=False)
async def registries_table(
    request: Request, db: Database, cli: Docker, _context: ManageSettings
) -> Response:
    """The table fragment, refreshed after a registry is added, changed or removed."""
    return render(request, TABLE_FRAGMENT, await _table_context(db, cli))


@router.get("/settings/registries/new", include_in_schema=False)
async def add_dialog(request: Request, _context: ManageSettings) -> Response:
    """The content of the add dialog: empty fields."""
    return render(request, FORM_FRAGMENT, _form_context("add"))


@router.post("/settings/registries", include_in_schema=False)
async def add_login(
    request: Request,
    db: Database,
    crypto: Crypto,
    context: ManageSettings,
    host: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
) -> Response:
    """Add a registry login. Refused values swap the dialog with the reasons (422)."""
    try:
        await add_registry(
            db,
            crypto,
            host,
            username,
            token,
            actor=context.user,
            ip_address=client_address(request),
        )
    except RegistryFormError as error:
        page = _form_context("add", host=host, username=username, errors=error.errors)
        return render(request, FORM_FRAGMENT, page, status_code=422)
    return events_response("registries-changed", "close-dialogs")


@router.get("/settings/registries/{registry_id:int}/edit", include_in_schema=False)
async def edit_dialog(
    request: Request, registry_id: int, db: Database, _context: ManageSettings
) -> Response:
    """The content of the edit dialog: the stored host and username and an empty token field."""
    row = await _stored_row(db, registry_id)
    if row is None:
        return _gone(request, "registry-edit")
    page = _form_context(
        "edit", registry_id=row.id, stored_host=row.host, host=row.host, username=row.username
    )
    return render(request, FORM_FRAGMENT, page)


@router.post("/settings/registries/{registry_id:int}/edit", include_in_schema=False)
async def edit_login(
    request: Request,
    registry_id: int,
    db: Database,
    crypto: Crypto,
    context: ManageSettings,
    host: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
) -> Response:
    """Change a registry login; an empty token keeps the stored one.

    Refused values swap the dialog with the reasons (422), a registry that is gone answers 404.
    """
    try:
        await update_registry(
            db,
            crypto,
            registry_id,
            host,
            username,
            token,
            actor=context.user,
            ip_address=client_address(request),
        )
    except RegistryNotFoundError:
        return _gone(request, "registry-edit")
    except RegistryFormError as error:
        stored = await _stored_row(db, registry_id)
        page = _form_context(
            "edit",
            registry_id=registry_id,
            stored_host=stored.host if stored is not None else host,
            host=host,
            username=username,
            errors=error.errors,
        )
        return render(request, FORM_FRAGMENT, page, status_code=422)
    return events_response("registries-changed", "close-dialogs")


@router.get("/settings/registries/{registry_id:int}/remove", include_in_schema=False)
async def remove_dialog(
    request: Request, registry_id: int, db: Database, _context: ManageSettings
) -> Response:
    """The confirm dialog of removing a registry login."""
    row = await _stored_row(db, registry_id)
    if row is None:
        return _gone(request, "registry-remove")
    return render(request, REMOVE_FRAGMENT, {"registry": row})


@router.post("/settings/registries/{registry_id:int}/remove", include_in_schema=False)
async def remove_login(
    request: Request, registry_id: int, db: Database, context: ManageSettings
) -> Response:
    """Remove a registry login and its stored token."""
    try:
        await remove_registry(
            db, registry_id, actor=context.user, ip_address=client_address(request)
        )
    except RegistryNotFoundError:
        return _gone(request, "registry-remove")
    return events_response("registries-changed", "close-dialogs")


@router.post("/settings/registries/{registry_id:int}/test", include_in_schema=False)
async def test_login(
    request: Request,
    registry_id: int,
    db: Database,
    crypto: Crypto,
    cli: Docker,
    context: ManageSettings,
) -> Response:
    """Test the stored login with ``docker login``; the answer is the result dialog content.

    The result is a 200 either way, so htmx swaps it; a registry that is gone answers 404.
    """
    row = await _stored_row(db, registry_id)
    if row is None:
        return _gone(request, "registry-test")
    try:
        check = await check_registry_login(
            db, crypto, cli, registry_id, actor=context.user, ip_address=client_address(request)
        )
    except RegistryNotFoundError:
        return _gone(request, "registry-test")
    return render(request, TEST_FRAGMENT, {"registry": row, "check": check})
