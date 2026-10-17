"""Service detail routes: the page and its refreshable panels."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import get_logger
from app.core.templating import render
from app.services.permissions import (
    CAPABILITY_GROUPS,
    CAPABILITY_TARGETS,
    AccessScope,
    Capability,
    ItemLevel,
    ItemRef,
    Target,
)
from app.services.service_detail import (
    ServiceHeader,
    load_environment_rows,
    load_post_deploy,
    load_service_header,
    load_service_history,
)
from app.web.dependencies import AuthContext, get_db, get_scope, require_auth

log = get_logger(__name__)
router = APIRouter()

ENVIRONMENTS_FRAGMENT = "fragments/service_environments.html"
POST_DEPLOY_FRAGMENT = "fragments/service_postdeploy.html"
HISTORY_FRAGMENT = "fragments/service_history.html"
ADDED_TOASTS = ("service", "environment")


@dataclass(frozen=True)
class ServiceContext:
    """The caller, their scope and the service as they see it."""

    auth: AuthContext
    scope: AccessScope
    service: ServiceHeader


def service_access(
    capability: Capability | None = None,
) -> Callable[..., Awaitable[ServiceContext]]:
    """A dependency that loads a service for the caller and checks an optional capability.

    A service outside the caller's scope answers 404, so its existence is not revealed; a
    capability the caller's group lacks on a service in scope answers 403 (capabilities that
    need an environment are not supported here).
    """

    async def load(
        service_id: int,
        auth: Annotated[AuthContext, Depends(require_auth)],
        scope: Annotated[AccessScope, Depends(get_scope)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> ServiceContext:
        service = await load_service_header(db, scope, service_id)
        if service is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
        if capability is not None:
            item = ItemRef(
                level=ItemLevel.SERVICE,
                project_id=service.project_id,
                deployment_id=service.deployment_id,
                service_id=service.id,
            )
            target = item if CAPABILITY_TARGETS[capability] is Target.ITEM else None
            if not scope.can(capability, target):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return ServiceContext(auth=auth, scope=scope, service=service)

    return load


def _flags(access: ServiceContext) -> dict[str, Any]:
    """What every panel template needs besides its rows: the service and what may be edited."""
    return {
        "service": access.service,
        "can_manage_structure": access.scope.can(Capability.MANAGE_STRUCTURE),
        "is_viewer": access.scope.group not in CAPABILITY_GROUPS[Capability.EDIT_VALUES],
    }


@router.get("/services/{service_id:int}", include_in_schema=False)
async def service_page(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    added: str = "",
) -> Response:
    """The service page: header, explanation, environments, post-deploy panel and history.

    ``added`` (service or environment) shows the toast after the add forms redirect here.
    """
    rows = await load_environment_rows(db, access.scope, access.service)
    history = await load_service_history(db, access.service)
    post_deploy = await load_post_deploy(db, access.service, history)
    return render(
        request,
        "pages/service.html",
        {
            **_flags(access),
            "environments": rows,
            "pending": sum(1 for row in rows if row.status == "pending"),
            "history": history,
            "post_deploy": post_deploy,
            "added": added if added in ADDED_TOASTS else "",
        },
    )


@router.get("/services/{service_id:int}/environments", include_in_schema=False)
async def environments_table(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The environments panel. A failed load answers an error panel with Retry."""
    try:
        rows = await load_environment_rows(db, access.scope, access.service)
    except SQLAlchemyError:
        log.exception("service environments could not be loaded")
        return render(
            request,
            ENVIRONMENTS_FRAGMENT,
            {**_flags(access), "environments": (), "load_failed": True},
            status_code=503,
        )
    return render(request, ENVIRONMENTS_FRAGMENT, {**_flags(access), "environments": rows})


@router.get("/services/{service_id:int}/postdeploy", include_in_schema=False)
async def post_deploy_panel(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The post-deploy panel, refreshed when a deploy finishes."""
    history = await load_service_history(db, access.service)
    post_deploy = await load_post_deploy(db, access.service, history)
    return render(request, POST_DEPLOY_FRAGMENT, {**_flags(access), "post_deploy": post_deploy})


@router.get("/services/{service_id:int}/history", include_in_schema=False)
async def service_history(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The deploy history panel, refreshed when a deploy finishes."""
    history = await load_service_history(db, access.service)
    return render(request, HISTORY_FRAGMENT, {**_flags(access), "history": history})
