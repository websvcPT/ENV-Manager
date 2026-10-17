"""Landing routes: the signed-in home and the projects page."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from app.core.templating import render
from app.web.dependencies import AuthContext, require_auth

router = APIRouter()


@router.get("/", include_in_schema=False, dependencies=[Depends(require_auth)])
async def home() -> RedirectResponse:
    """Send a signed-in user to the projects page, where login lands."""
    return RedirectResponse("/projects", status_code=303)


@router.get("/projects", include_in_schema=False)
async def projects_page(
    request: Request, _context: Annotated[AuthContext, Depends(require_auth)]
) -> Response:
    """The projects page inside the application shell."""
    return render(request, "pages/projects.html")
