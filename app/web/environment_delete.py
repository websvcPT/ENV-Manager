"""Environment delete routes: the confirmation dialog and its submit. Superadmin only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Environment
from app.services.environment_delete import EnvironmentInUseError, delete_environment
from app.services.permissions import Capability
from app.web.dependencies import get_db
from app.web.editor import EnvironmentContext, environment_access
from app.web.htmx import hx_redirect

router = APIRouter()

DELETE_FRAGMENT = "fragments/environment_delete_form.html"
DELETE_CONFIRM_ERROR = "Type the file name exactly to confirm."

Structure = Annotated[EnvironmentContext, Depends(environment_access(Capability.MANAGE_STRUCTURE))]


@router.get("/environments/{environment_id:int}/delete", include_in_schema=False)
async def delete_form(request: Request, access: Structure) -> Response:
    """The delete confirmation, loaded into its dialog when it opens."""
    return render(request, DELETE_FRAGMENT, {"env": access.env})


@router.post("/environments/{environment_id:int}/delete", include_in_schema=False)
async def delete_route(
    request: Request,
    access: Structure,
    db: Annotated[AsyncSession, Depends(get_db)],
    confirm_name: Annotated[str, Form()] = "",
) -> Response:
    """Delete the file after its name was typed back. Success leaves for the service.

    A wrong name or API tokens still restricted to the file swap the dialog form again with the
    reason (422); nothing is deleted.
    """
    environment = await db.get(Environment, access.env.id)
    if environment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "environment not found")
    error: str | None = None
    if confirm_name.strip() != environment.name:
        error = DELETE_CONFIRM_ERROR
    else:
        try:
            await delete_environment(
                db,
                environment,
                access.env,
                actor=access.auth.user,
                ip_address=client_address(request),
            )
        except EnvironmentInUseError as refused:
            error = str(refused)
    if error is not None:
        return render(
            request,
            DELETE_FRAGMENT,
            {"env": access.env, "form_value": confirm_name, "form_error": error},
            status_code=422,
        )
    return hx_redirect(f"/services/{access.env.service_id}?deleted=environment")
