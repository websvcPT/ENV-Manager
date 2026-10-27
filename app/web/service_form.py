"""Service structure routes: the add form, rename and delete. Superadmin only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Deployment, Service
from app.services.permissions import Capability
from app.services.services import (
    ServiceInUseError,
    ServiceNameError,
    create_service,
    delete_service,
    ensure_deployment_files,
    rename_service,
)
from app.web.dependencies import get_db
from app.web.deployments import DeploymentContext, deployment_access
from app.web.htmx import hx_redirect
from app.web.service_detail import ServiceContext, service_access

router = APIRouter()

PAGE = "pages/service_form.html"
FORM_FRAGMENT = "fragments/service_form.html"
RENAME_PAGE = "pages/service_rename.html"
RENAME_FRAGMENT = "fragments/service_rename_form.html"
DELETE_FRAGMENT = "fragments/service_delete_form.html"
DELETE_CONFIRM_ERROR = "Type the service name exactly to confirm."


@router.get("/deployments/{deployment_id:int}/services/new", include_in_schema=False)
async def new_service_page(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    name: str = "",
) -> Response:
    """The add service form, with the name when the address carries one (a service the Docker
    Compose project has that the application does not)."""
    return render(request, PAGE, {"deployment": access.deployment, "name": name, "error": None})


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
            db, deployment, name, actor=access.auth.user, ip_address=client_address(request)
        )
    except ServiceNameError as error:
        return render(
            request,
            FORM_FRAGMENT,
            {"deployment": access.deployment, "name": name, "error": str(error)},
            status_code=422,
        )
    return hx_redirect(f"/services/{service.id}?added=service")


@router.post("/deployments/{deployment_id:int}/deployment-files", include_in_schema=False)
async def open_deployment_files(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Create the deployment's own files on first use (audited) and open the add file form."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    files = await ensure_deployment_files(
        db, deployment, actor=access.auth.user, ip_address=client_address(request)
    )
    await db.commit()
    return hx_redirect(f"/services/{files.id}/environments/new")


async def _stored_service(db: AsyncSession, access: ServiceContext) -> Service:
    """The stored service the authorised context was loaded from."""
    service = await db.get(Service, access.service.id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    return service


@router.get("/services/{service_id:int}/rename", include_in_schema=False)
async def rename_service_page(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
) -> Response:
    """The rename form with the current name."""
    return render(
        request,
        RENAME_PAGE,
        {"service": access.service, "name": access.service.name, "error": None},
    )


@router.post("/services/{service_id:int}/rename", include_in_schema=False)
async def rename_service_route(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()] = "",
) -> Response:
    """Rename the service. A refused name swaps the form again with the reason (422)."""
    service = await _stored_service(db, access)
    try:
        await rename_service(
            db, service, name, actor=access.auth.user, ip_address=client_address(request)
        )
    except ServiceNameError as error:
        return render(
            request,
            RENAME_FRAGMENT,
            {"service": access.service, "name": name, "error": str(error)},
            status_code=422,
        )
    return hx_redirect(f"/services/{service.id}?renamed=true")


@router.get("/services/{service_id:int}/delete", include_in_schema=False)
async def delete_service_form(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
) -> Response:
    """The delete confirmation, loaded into its dialog when it opens."""
    return render(request, DELETE_FRAGMENT, {"service": access.service})


@router.post("/services/{service_id:int}/delete", include_in_schema=False)
async def delete_service_route(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    confirm_name: Annotated[str, Form()] = "",
) -> Response:
    """Delete the service after its name was typed back. Success leaves for the deployment.

    A wrong name or API tokens still restricted to the service swap the dialog form again with
    the reason (422); nothing is deleted.
    """
    service = await _stored_service(db, access)
    error: str | None = None
    if confirm_name.strip() != service.name:
        error = DELETE_CONFIRM_ERROR
    else:
        try:
            await delete_service(
                db, service, actor=access.auth.user, ip_address=client_address(request)
            )
        except (ServiceInUseError, ServiceNameError) as refused:
            error = str(refused)
    if error is not None:
        return render(
            request,
            DELETE_FRAGMENT,
            {"service": access.service, "form_value": confirm_name, "form_error": error},
            status_code=422,
        )
    return hx_redirect(f"/deployments/{access.service.deployment_id}/files?deleted=service")
