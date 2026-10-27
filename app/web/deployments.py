"""Deployment detail routes: the Overview, Files, History and Settings tabs and the refreshable
services and history panels."""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Deployment
from app.services.access_grid import NodeKind
from app.services.deployment_detail import DeploymentDetail, load_deployment_detail
from app.services.deployments import DeploymentInUseError, delete_deployment
from app.services.paths import AllowedPaths
from app.services.permissions import AccessScope, Capability, ItemLevel, ItemRef, Visibility
from app.web.access_tabs import AccessSubject, mount_access_routes
from app.web.dependencies import AuthContext, get_db, item_access
from app.web.htmx import hx_redirect
from app.web.params import plain_number_text

router = APIRouter()

DELETE_FRAGMENT = "fragments/deployment_delete_form.html"
DELETE_CONFIRM_ERROR = "Type the deployment name exactly to confirm."
IMPORTED_FIELDS = 6


@dataclass(frozen=True)
class DeploymentContext:
    """The caller, their scope and the deployment as they see it."""

    auth: AuthContext
    scope: AccessScope
    deployment: DeploymentDetail


async def _load_deployment(
    db: AsyncSession,
    scope: AccessScope,
    deployment_id: int,
    visibility: Visibility,
    allowed: AllowedPaths,
) -> DeploymentDetail | None:
    """The deployment as the caller sees it, with what the request already holds."""
    return await load_deployment_detail(db, scope, deployment_id, allowed, visibility)


# Capabilities that need an environment are not supported on a deployment.
deployment_access = item_access(
    "deployment_id",
    _load_deployment,
    lambda deployment: ItemRef(
        level=ItemLevel.DEPLOYMENT, project_id=deployment.project_id, deployment_id=deployment.id
    ),
    "deployment not found",
    DeploymentContext,
)


def _counted(number: int, noun: str) -> str:
    """The count with its noun, the noun made plural unless the count is one."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def imported_message(imported: str) -> str:
    """The toast text for the counts an import left in the address, empty when they are not six
    plain numbers (services, files, files with content, files created empty, items not imported,
    post-deploy commands set).

    The numbers come from the address and are only echoed, as the version of a restore toast is.
    """
    parts = imported.split(",")
    if len(parts) != IMPORTED_FIELDS or not all(
        part and plain_number_text(part) == part for part in parts
    ):
        return ""
    services, files, with_content, empty, skipped, commands = (int(part) for part in parts)
    text = (
        f"Imported {_counted(services, 'service')} and {_counted(files, 'file')} "
        f"({with_content} with content, {empty} created empty)."
    )
    if skipped:
        text += f" {_counted(skipped, 'item')} not imported."
    if commands:
        text += f" {_counted(commands, 'post-deploy command')} set."
    return text


def compose_linked_message(counts: str) -> str:
    """The toast text for the counts a link of Compose files left in the address (files linked,
    deployment files removed), empty when they are not two plain numbers."""
    parts = counts.split(",")
    if len(parts) != 2 or not all(part and plain_number_text(part) == part for part in parts):
        return ""
    linked, removed = (int(part) for part in parts)
    text = f"{_counted(linked, 'Compose file')} linked." if linked else "Compose files unlinked."
    if removed:
        text += f" {_counted(removed, 'file')} removed from the deployment files."
    return text


def _page_context(access: DeploymentContext, tab: str, **extra: Any) -> dict[str, Any]:
    """What a tab page and its panels need: the deployment, the open tab and what the caller may
    do."""
    return {
        "deployment": access.deployment,
        "tab": tab,
        "can_manage_structure": access.scope.can(Capability.MANAGE_STRUCTURE),
        "compose_deployment_id": access.deployment.id,
        "compose_files": access.deployment.compose_files,
        "can_view_compose": access.deployment.can_view_compose,
        "can_link_compose": (
            access.scope.can(Capability.MANAGE_STRUCTURE)
            and access.deployment.full
            and not access.deployment.outside_allowed
        ),
        "can_manage_grants": access.scope.can(Capability.MANAGE_GRANTS),
        "can_view_output": access.deployment.can_view_output,
        **extra,
    }


async def _access_subject(
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_GRANTS))],
) -> AccessSubject:
    """The deployment of an Access tab request, for the superadmin."""
    return AccessSubject(
        NodeKind.DEPLOYMENT,
        access.deployment.id,
        access.deployment.project_id,
        access.auth,
        "pages/deployment_access.html",
        _page_context(access, "access"),
    )


mount_access_routes(router, NodeKind.DEPLOYMENT, _access_subject)


async def _whole_deployment(
    access: Annotated[DeploymentContext, Depends(deployment_access())],
) -> DeploymentContext:
    """The deployment for a caller who sees all of it; a caller who reaches it only through a
    granted environment is refused (403), since the settings are not theirs to read."""
    if not access.deployment.full:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
    return access


@router.get("/deployments/{deployment_id:int}", include_in_schema=False)
async def deployment_page(
    request: Request, access: Annotated[DeploymentContext, Depends(deployment_access())]
) -> Response:
    """The Overview tab: the status strip, the services in brief and the newest deploys."""
    return render(request, "pages/deployment.html", _page_context(access, "overview"))


@router.get("/deployments/{deployment_id:int}/files", include_in_schema=False)
async def files_tab(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access())],
    deleted: str = "",
    imported: str = "",
) -> Response:
    """The Files tab: the services with their actions and the deployment files.

    ``deleted`` (service) shows the toast after a service of the deployment was deleted and
    ``imported`` (the counts) the one after a Docker Compose import.
    """
    return render(
        request,
        "pages/deployment_files.html",
        _page_context(
            access,
            "files",
            deleted="service" if deleted == "service" else "",
            imported=imported_message(imported),
        ),
    )


@router.get("/deployments/{deployment_id:int}/activity", include_in_schema=False)
async def activity_tab(
    request: Request, access: Annotated[DeploymentContext, Depends(deployment_access())]
) -> Response:
    """The History tab: the deploy history across the services in the caller's scope."""
    return render(request, "pages/deployment_activity.html", _page_context(access, "activity"))


@router.get("/deployments/{deployment_id:int}/settings", include_in_schema=False)
async def settings_tab(
    request: Request,
    access: Annotated[DeploymentContext, Depends(_whole_deployment)],
    saved: bool = False,
    compose_linked: str = "",
) -> Response:
    """The Settings tab: the policies, the Compose files, Edit and Delete.

    ``saved`` shows the toast after the edit form saved and ``compose_linked`` (the counts) the
    one after the Compose files were linked.
    """
    return render(
        request,
        "pages/deployment_settings.html",
        _page_context(
            access,
            "settings",
            saved=saved,
            compose_linked=compose_linked_message(compose_linked),
        ),
    )


@router.get("/deployments/{deployment_id:int}/delete", include_in_schema=False)
async def delete_deployment_form(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
) -> Response:
    """The delete confirmation, loaded into its dialog when it opens."""
    return render(request, DELETE_FRAGMENT, {"deployment": access.deployment})


@router.post("/deployments/{deployment_id:int}/delete", include_in_schema=False)
async def delete_deployment_route(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    confirm_name: Annotated[str, Form()] = "",
) -> Response:
    """Delete the deployment after its name was typed back. Success leaves for the project.

    A wrong name or API tokens still restricted to the deployment swap the dialog form again
    with the reason (422); nothing is deleted.
    """
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    error: str | None = None
    if confirm_name.strip() != deployment.name:
        error = DELETE_CONFIRM_ERROR
    else:
        try:
            await delete_deployment(
                db, deployment, actor=access.auth.user, ip_address=client_address(request)
            )
        except DeploymentInUseError as refused:
            error = str(refused)
    if error is not None:
        return render(
            request,
            DELETE_FRAGMENT,
            {"deployment": access.deployment, "form_value": confirm_name, "form_error": error},
            status_code=422,
        )
    return hx_redirect(f"/projects/{access.deployment.project_id}?deleted=deployment")


@router.get("/deployments/{deployment_id:int}/services", include_in_schema=False)
async def services_table(
    request: Request, access: Annotated[DeploymentContext, Depends(deployment_access())]
) -> Response:
    """The services panel, refreshed when a deploy finishes."""
    return render(request, "fragments/services_table.html", _page_context(access, "files"))


@router.get("/deployments/{deployment_id:int}/history", include_in_schema=False)
async def deploy_history(
    request: Request, access: Annotated[DeploymentContext, Depends(deployment_access())]
) -> Response:
    """The deploy history panel, refreshed when a deploy finishes."""
    return render(request, "fragments/deploy_history.html", _page_context(access, "activity"))
