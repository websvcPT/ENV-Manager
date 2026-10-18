"""Deployment detail routes: the page and its refreshable services and history panels."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.services.access_grid import NodeKind
from app.services.deployment_detail import DeploymentDetail, load_deployment_detail
from app.services.paths import load_allowed_paths
from app.services.permissions import (
    CAPABILITY_TARGETS,
    AccessScope,
    Capability,
    ItemLevel,
    ItemRef,
    Target,
)
from app.web.access_tabs import AccessSubject, mount_access_routes
from app.web.dependencies import AuthContext, get_db, get_scope, require_auth

router = APIRouter()


@dataclass(frozen=True)
class DeploymentContext:
    """The caller, their scope and the deployment as they see it."""

    auth: AuthContext
    scope: AccessScope
    deployment: DeploymentDetail


def deployment_access(
    capability: Capability | None = None,
) -> Callable[..., Awaitable[DeploymentContext]]:
    """A dependency that loads a deployment for the caller and checks an optional capability.

    A deployment outside the caller's scope answers 404, so its existence is not revealed; a
    capability the caller's group lacks on a deployment in scope answers 403 (capabilities that
    need an environment are not supported here). The allowed paths are loaded fresh for every
    request.
    """

    async def load(
        deployment_id: int,
        auth: Annotated[AuthContext, Depends(require_auth)],
        scope: Annotated[AccessScope, Depends(get_scope)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> DeploymentContext:
        allowed = await load_allowed_paths(db)
        deployment = await load_deployment_detail(db, scope, deployment_id, allowed)
        if deployment is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
        if capability is not None:
            item = ItemRef(
                level=ItemLevel.DEPLOYMENT,
                project_id=deployment.project_id,
                deployment_id=deployment.id,
            )
            target = item if CAPABILITY_TARGETS[capability] is Target.ITEM else None
            if not scope.can(capability, target):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return DeploymentContext(auth=auth, scope=scope, deployment=deployment)

    return load


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
