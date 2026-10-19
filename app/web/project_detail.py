"""Project detail routes: the four tabs, rename, the extra notification addresses and delete."""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Project
from app.services.access_grid import NodeKind
from app.services.paths import AllowedPaths
from app.services.permissions import AccessScope, Capability, ItemLevel, ItemRef, Visibility
from app.services.project_addresses import AddressesError, save_extra_addresses
from app.services.project_detail import (
    ProjectDetail,
    list_activity,
    load_project_detail,
)
from app.services.projects import (
    NAME_RULE,
    ProjectError,
    ProjectInUseError,
    delete_project,
    rename_project,
)
from app.web.access_tabs import AccessSubject, mount_access_routes
from app.web.dependencies import AuthContext, get_db, get_visibility, item_access
from app.web.htmx import hx_redirect

router = APIRouter()

RENAME_FRAGMENT = "fragments/project_rename.html"
ADDRESSES_FRAGMENT = "fragments/project_addresses.html"
DELETE_FRAGMENT = "fragments/project_delete_form.html"
DELETE_CONFIRM_ERROR = "Type the project name exactly to confirm."


@dataclass(frozen=True)
class ProjectContext:
    """The caller, their scope and the project as they see it."""

    auth: AuthContext
    scope: AccessScope
    project: ProjectDetail


async def _load_project(
    db: AsyncSession,
    scope: AccessScope,
    project_id: int,
    visibility: Visibility,
    allowed: AllowedPaths,
) -> ProjectDetail | None:
    """The project as the caller sees it, with what the request already holds."""
    return await load_project_detail(db, scope, project_id, allowed, visibility)


project_access = item_access(
    "project_id",
    _load_project,
    lambda project: ItemRef(level=ItemLevel.PROJECT, project_id=project.id),
    "project not found",
    ProjectContext,
)


def _project_row(access: ProjectContext) -> Project:
    """The stored project the authorised context was loaded from."""
    return access.project.row


def _page_context(access: ProjectContext, tab: str, **extra: Any) -> dict[str, Any]:
    """What every tab page needs: the project, the open tab and what the caller may manage."""
    return {
        "project": access.project,
        "tab": tab,
        "can_manage_structure": access.scope.can(Capability.MANAGE_STRUCTURE),
        "can_manage_grants": access.scope.can(Capability.MANAGE_GRANTS),
        "can_view_audit": access.scope.can(Capability.VIEW_AUDIT),
        "name_rule": NAME_RULE,
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
    visibility: Annotated[Visibility, Depends(get_visibility)],
) -> Response:
    """The Activity tab: the project's newest audit entries in the caller's scope, admins only."""
    rows = await list_activity(db, access.scope, access.project.id, visibility=visibility)
    context = _page_context(access, "activity", rows=rows)
    return render(request, "pages/project_activity.html", context)


async def _access_subject(
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_GRANTS))],
) -> AccessSubject:
    """The project of an Access tab request, for the superadmin."""
    return AccessSubject(
        NodeKind.PROJECT,
        access.project.id,
        access.project.id,
        access.auth,
        "pages/project_access.html",
        _page_context(access, "access"),
    )


mount_access_routes(router, NodeKind.PROJECT, _access_subject)


@router.get("/projects/{project_id:int}/members", include_in_schema=False)
async def members_redirect(
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_GRANTS))],
) -> Response:
    """The old address of the Members tab, which is the Access tab now."""
    return RedirectResponse(
        f"/projects/{access.project.id}/access",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/projects/{project_id:int}/settings", include_in_schema=False)
async def settings_tab(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    renamed: bool = False,
) -> Response:
    """The Settings tab (superadmin): rename, the extra notification addresses and delete."""
    project = _project_row(access)
    return render(
        request,
        "pages/project_settings.html",
        _page_context(
            access,
            "settings",
            renamed=renamed,
            addresses_text="\n".join(project.extra_addresses or []),
        ),
    )


@router.post("/projects/{project_id:int}/notification-addresses", include_in_schema=False)
async def addresses_route(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    addresses: Annotated[str, Form()] = "",
) -> Response:
    """Save the extra notification addresses. A refused list answers the panel with the reason
    (422); nothing is stored then."""
    project = _project_row(access)
    try:
        change = await save_extra_addresses(
            db, project, addresses, actor=access.auth.user, ip_address=client_address(request)
        )
    except AddressesError as error:
        page = _page_context(
            access, "settings", addresses_text=addresses, addresses_error=str(error)
        )
        return render(request, ADDRESSES_FRAGMENT, page, status_code=422)
    if change.changed:
        saved = {
            "tone": "success",
            "title": "Addresses saved",
            "message": "They apply to the next notification.",
        }
    else:
        saved = {
            "tone": "info",
            "title": "Nothing to save",
            "message": "The list is unchanged.",
        }
    page = _page_context(
        access,
        "settings",
        addresses_text="\n".join(change.addresses),
        addresses_saved=saved,
    )
    return render(request, ADDRESSES_FRAGMENT, page)


@router.post("/projects/{project_id:int}/rename", include_in_schema=False)
async def rename_route(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()] = "",
) -> Response:
    """Rename the project. Success reloads the settings tab so every title shows the new name."""
    project = _project_row(access)
    try:
        await rename_project(
            db, project, name=name, actor=access.auth.user, ip_address=client_address(request)
        )
    except ProjectError as error:
        return render(
            request,
            RENAME_FRAGMENT,
            _page_context(access, "settings", form_value=name.strip(), form_error=str(error)),
            status_code=422,
        )
    return hx_redirect(f"/projects/{project.id}/settings?renamed=true")


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
    project = _project_row(access)
    error: str | None = None
    if confirm_name.strip() != project.name:
        error = DELETE_CONFIRM_ERROR
    else:
        try:
            await delete_project(
                db, project, actor=access.auth.user, ip_address=client_address(request)
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
    return hx_redirect("/projects")
