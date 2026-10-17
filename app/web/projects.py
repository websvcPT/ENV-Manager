"""Project routes: the landing redirect, the projects list with its table fragment, and create."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import get_logger
from app.core.templating import render
from app.services.permissions import AccessScope, Capability
from app.services.projects import NAME_RULE, ProjectError, create_project, list_project_rows
from app.web.dependencies import (
    AuthContext,
    get_db,
    get_scope,
    require_auth,
    require_capability,
)

log = get_logger("web.projects")

router = APIRouter()

TABLE_FRAGMENT = "fragments/projects_table.html"
FORM_FRAGMENT = "fragments/project_new_form.html"


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


async def _table_context(db: AsyncSession, scope: AccessScope, query: str) -> dict[str, Any]:
    """What the projects table fragment needs: rows plus what this caller may do with them."""
    return {
        "rows": await list_project_rows(db, scope, query=query),
        "query": query.strip(),
        "can_create": scope.can(Capability.MANAGE_STRUCTURE),
        "show_members": scope.can(Capability.MANAGE_GRANTS),
    }


@router.get("/", include_in_schema=False, dependencies=[Depends(require_auth)])
async def home() -> RedirectResponse:
    """Send a signed-in user to the projects page, where login lands."""
    return RedirectResponse("/projects", status_code=303)


@router.get("/projects", include_in_schema=False)
async def projects_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[AccessScope, Depends(get_scope)],
    _context: Annotated[AuthContext, Depends(require_auth)],
    q: str = "",
) -> Response:
    """The projects page: every project in the caller's scope, optionally filtered by name."""
    context = await _table_context(db, scope, q)
    return render(request, "pages/projects.html", {**context, "name_rule": NAME_RULE})


@router.get("/projects/table", include_in_schema=False)
async def projects_table(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[AccessScope, Depends(get_scope)],
    _context: Annotated[AuthContext, Depends(require_auth)],
    q: str = "",
) -> Response:
    """The projects table fragment. A failed load answers an error panel with Retry."""
    try:
        context = await _table_context(db, scope, q)
    except SQLAlchemyError:
        log.exception("projects table could not be loaded")
        return render(
            request,
            TABLE_FRAGMENT,
            {
                "load_failed": True,
                "rows": [],
                "query": "",
                "can_create": False,
                "show_members": False,
            },
            status_code=503,
        )
    return render(request, TABLE_FRAGMENT, context)


@router.post("/projects", include_in_schema=False)
async def create_project_route(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[AccessScope, Depends(get_scope)],
    context: Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_STRUCTURE))],
    name: Annotated[str, Form()] = "",
) -> Response:
    """Create a project (superadmin only).

    Success swaps in the refreshed table, resets the form and closes the dialog; a refused name
    swaps the form again with the reason.
    """
    try:
        await create_project(db, name=name, actor=context.user, ip_address=_client_ip(request))
    except ProjectError as error:
        return render(
            request,
            FORM_FRAGMENT,
            {"form_value": name.strip(), "form_error": str(error), "name_rule": NAME_RULE},
            status_code=422,
        )
    table = render(request, TABLE_FRAGMENT, await _table_context(db, scope, ""))
    fresh_form = render(request, FORM_FRAGMENT, {"form_oob": True, "name_rule": NAME_RULE})
    return HTMLResponse(
        bytes(table.body) + bytes(fresh_form.body),
        headers={
            "Cache-Control": "no-store",
            "Vary": "HX-Request",
            "HX-Trigger": "close-dialogs",
        },
    )
