"""Access grid routes (superadmin only): the page, its table for a filter and cell toggles.

Every answer is for the superadmin, who manages grants; any other group gets a 403 on all
three routes. A toggle goes through the grants service, so it is audited per grant added or
removed and applies at once, and the server decides what the grid may do whatever a client
posts: inherited cells, deleted items, superadmins and out of date pages are refused.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import User
from app.services.access_grid import GridFilters, load_access_grid, toggle_cell
from app.services.audit import AuditActor
from app.services.grants import MAX_ID_DIGITS, GrantTarget
from app.services.permissions import Capability
from app.web.dependencies import AuthContext, get_db, require_capability

router = APIRouter()

GRID_PATH = "/access/grid"
ManageGrants = Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_GRANTS))]
Database = Annotated[AsyncSession, Depends(get_db)]


@router.get(GRID_PATH, include_in_schema=False)
async def grid_page(request: Request, db: Database, _context: ManageGrants) -> Response:
    """The access grid under the filters of the query string."""
    filters = GridFilters.parse(request.query_params)
    return render(request, "pages/access_grid.html", {"grid": await load_access_grid(db, filters)})


@router.get(f"{GRID_PATH}/table", include_in_schema=False)
async def grid_table(request: Request, db: Database, _context: ManageGrants) -> Response:
    """The table for the filters of the form, with ``HX-Push-Url`` to the same view as a page."""
    grid = await load_access_grid(db, GridFilters.parse(request.query_params))
    response = render(request, "fragments/grid_table.html", {"grid": grid})
    query = grid.filters.query_string()
    response.headers["HX-Push-Url"] = f"{GRID_PATH}?{query}" if query else GRID_PATH
    return response


@router.post(f"{GRID_PATH}/toggle", include_in_schema=False)
async def toggle_route(
    request: Request,
    db: Database,
    context: ManageGrants,
    user: Annotated[int, Form(ge=1, lt=10**MAX_ID_DIGITS)],
    target: Annotated[str, Form()],
    was: Annotated[str, Form()] = "0",
) -> Response:
    """Flip one direct grant and answer the cells to redraw.

    ``was`` is the state the clicked cell showed. A refusal (stale cell, inherited access, item
    gone, superadmin) is a 409 that redraws the user's column of the project as stored now with
    the reason on the clicked cell. A change tells other panels with ``grants-changed``.
    """
    grant_target = GrantTarget.parse(target)
    if grant_target is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "not a grantable item")
    row = await db.get(User, user)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    update = await toggle_cell(
        db,
        row,
        grant_target,
        was=was == "1",
        actor=AuditActor.for_user(context.user, client_address(request)),
    )
    response = render(
        request,
        "fragments/grid_cells.html",
        {"update": update},
        status_code=status.HTTP_409_CONFLICT if update.notice else status.HTTP_200_OK,
    )
    if update.changed:
        response.headers["HX-Trigger"] = json.dumps({"grants-changed": True})
    return response
