"""Deployment detail routes: the page and its refreshable services and history panels."""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.services.access_grid import NodeKind
from app.services.deployment_detail import DeploymentDetail, load_deployment_detail
from app.services.paths import AllowedPaths
from app.services.permissions import AccessScope, Capability, ItemLevel, ItemRef, Visibility
from app.web.access_tabs import AccessSubject, mount_access_routes
from app.web.dependencies import AuthContext, item_access

router = APIRouter()


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


def _page_context(access: DeploymentContext, **extra: Any) -> dict[str, Any]:
    """What the detail page and its panels need: the deployment and what the caller may do."""
    return {
        "deployment": access.deployment,
        "can_manage_structure": access.scope.can(Capability.MANAGE_STRUCTURE),
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
        _page_context(access),
    )


mount_access_routes(router, NodeKind.DEPLOYMENT, _access_subject)


@router.get("/deployments/{deployment_id:int}", include_in_schema=False)
async def deployment_page(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access())],
    saved: bool = False,
) -> Response:
    """The deployment detail page; ``saved`` shows the toast after the edit form saved."""
    return render(request, "pages/deployment.html", _page_context(access, saved=saved))


@router.get("/deployments/{deployment_id:int}/services", include_in_schema=False)
async def services_table(
    request: Request, access: Annotated[DeploymentContext, Depends(deployment_access())]
) -> Response:
    """The services panel, refreshed when a deploy finishes."""
    return render(request, "fragments/services_table.html", _page_context(access))


@router.get("/deployments/{deployment_id:int}/history", include_in_schema=False)
async def deploy_history(
    request: Request, access: Annotated[DeploymentContext, Depends(deployment_access())]
) -> Response:
    """The deploy history panel, refreshed when a deploy finishes."""
    return render(request, "fragments/deploy_history.html", _page_context(access))
