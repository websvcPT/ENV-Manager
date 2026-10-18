"""Project detail routes: the four tabs, rename and delete."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.models import Project
from app.services.paths import load_allowed_paths
from app.services.permissions import AccessScope, Capability
from app.services.project_detail import (
    ProjectDetail,
    list_activity,
    list_members,
    load_project_detail,
)
from app.services.projects import (
    ProjectError,
    ProjectInUseError,
    delete_project,
    rename_project,
)
from app.web.dependencies import AuthContext, get_db, get_scope, require_auth

router = APIRouter()

RENAME_FRAGMENT = "fragments/project_rename.html"
DELETE_FRAGMENT = "fragments/project_delete_form.html"
DELETE_CONFIRM_ERROR = "Type the project name exactly to confirm."


@dataclass(frozen=True)
class ProjectContext:
    """The caller, their scope and the project as they see it."""

    auth: AuthContext
    scope: AccessScope
    project: ProjectDetail


def project_access(
    capability: Capability | None = None,
) -> Callable[..., Awaitable[ProjectContext]]:
    """A dependency that loads a project for the caller and checks an optional capability.

    A project outside the caller's scope answers 404, so its existence is not revealed; a
    capability the caller's group lacks on a project in scope answers 403.
    """

    async def load(
        project_id: int,
        auth: Annotated[AuthContext, Depends(require_auth)],
        scope: Annotated[AccessScope, Depends(get_scope)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> ProjectContext:
        project = await load_project_detail(db, scope, project_id, await load_allowed_paths(db))
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
        if capability is not None and not scope.can(capability):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return ProjectContext(auth=auth, scope=scope, project=project)

    return load


async def _project_row(db: AsyncSession, access: ProjectContext) -> Project:
    """The project row behind an already authorised context."""
    project = await db.get(Project, access.project.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _page_context(access: ProjectContext, tab: str, **extra: Any) -> dict[str, Any]:
    """What every tab page needs: the project, the open tab and what the caller may manage."""
    return {
        "project": access.project,
        "tab": tab,
        "can_manage_structure": access.scope.can(Capability.MANAGE_STRUCTURE),
        "can_manage_grants": access.scope.can(Capability.MANAGE_GRANTS),
        "can_view_audit": access.scope.can(Capability.VIEW_AUDIT),
        **extra,
    }


@router.get("/projects/{project_id:int}", include_in_schema=False)
async def project_page(
    request: Request, access: Annotated[ProjectContext, Depends(project_access())]
) -> Response:
    """The Deployments tab."""
    return render(request, "pages/project.html", _page_context(access, "deployments"))


@router.get("/projects/{project_id:int}/deployments", include_in_schema=False)
async def deployments_table(
    request: Request, access: Annotated[ProjectContext, Depends(project_access())]
) -> Response:
    """The deployments panel, refreshed when a deploy finishes."""
    return render(request, "fragments/deployments_table.html", _page_context(access, "deployments"))


@router.get("/projects/{project_id:int}/activity", include_in_schema=False)
async def activity_tab(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.VIEW_AUDIT))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The Activity tab: the project's newest audit entries in the caller's scope, admins only."""
    rows = await list_activity(db, access.scope, access.project.id)
    context = _page_context(access, "activity", rows=rows)
    return render(request, "pages/project_activity.html", context)


@router.get("/projects/{project_id:int}/members", include_in_schema=False)
async def members_tab(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_GRANTS))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The Members and access tab (superadmin)."""
    members = await list_members(db, access.project.id)
    return render(
        request, "pages/project_members.html", _page_context(access, "members", members=members)
    )


@router.get("/projects/{project_id:int}/settings", include_in_schema=False)
async def settings_tab(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    renamed: bool = False,
) -> Response:
    """The Settings tab (superadmin): rename and delete."""
    return render(
        request,
        "pages/project_settings.html",
        _page_context(access, "settings", renamed=renamed),
    )


@router.post("/projects/{project_id:int}/rename", include_in_schema=False)
async def rename_route(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()] = "",
) -> Response:
    """Rename the project. Success reloads the settings tab so every title shows the new name."""
    project = await _project_row(db, access)
    try:
        await rename_project(
            db, project, name=name, actor=access.auth.user, ip_address=_client_ip(request)
        )
    except ProjectError as error:
        return render(
            request,
            RENAME_FRAGMENT,
            _page_context(access, "settings", form_value=name.strip(), form_error=str(error)),
            status_code=422,
        )
    return Response(
        status_code=200,
        headers={
            "HX-Redirect": f"/projects/{project.id}/settings?renamed=true",
            "Cache-Control": "no-store",
            "Vary": "HX-Request",
        },
    )


@router.post("/projects/{project_id:int}/delete", include_in_schema=False)
async def delete_route(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    confirm_name: Annotated[str, Form()] = "",
) -> Response:
    """Delete the project after its name was typed back. Success leaves for the projects list.

    A wrong name or API tokens still restricted to the project swap the dialog form again with
    the reason; nothing is deleted.
    """
    project = await _project_row(db, access)
    error: str | None = None
    if confirm_name.strip() != project.name:
        error = DELETE_CONFIRM_ERROR
    else:
        try:
            await delete_project(
                db, project, actor=access.auth.user, ip_address=_client_ip(request)
            )
        except ProjectInUseError as refused:
            error = str(refused)
    if error is not None:
        return render(
            request,
            DELETE_FRAGMENT,
            _page_context(access, "settings", form_value=confirm_name, form_error=error),
            status_code=422,
        )
    return Response(
        status_code=200,
        headers={"HX-Redirect": "/projects", "Cache-Control": "no-store", "Vary": "HX-Request"},
    )
