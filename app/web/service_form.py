"""Add service routes: the form page and its submit. Superadmin only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.models import Deployment
from app.services.permissions import Capability
from app.services.services import ServiceNameError, create_service
from app.web.dependencies import get_db
from app.web.deployments import DeploymentContext, deployment_access

router = APIRouter()

PAGE = "pages/service_form.html"
FORM_FRAGMENT = "fragments/service_form.html"


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _redirect(url: str) -> Response:
    """Tell htmx to navigate to ``url``."""
    return Response(
        status_code=200,
        headers={"HX-Redirect": url, "Cache-Control": "no-store", "Vary": "HX-Request"},
    )


@router.get("/deployments/{deployment_id:int}/services/new", include_in_schema=False)
async def new_service_page(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
) -> Response:
    """The empty add service form."""
    return render(request, PAGE, {"deployment": access.deployment, "name": "", "error": None})


@router.post("/deployments/{deployment_id:int}/services/new", include_in_schema=False)
async def create_service_route(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()] = "",
) -> Response:
    """Create a service. A refused name swaps the form again with the reason (422)."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    try:
        service = await create_service(
            db, deployment, name, actor=access.auth.user, ip_address=_client_ip(request)
        )
    except ServiceNameError as error:
        return render(
            request,
            FORM_FRAGMENT,
            {"deployment": access.deployment, "name": name, "error": str(error)},
            status_code=422,
        )
    return _redirect(f"/services/{service.id}?added=service")
