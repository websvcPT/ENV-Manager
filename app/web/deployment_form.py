"""Deployment form routes: create, edit, root path validation and the path picker.

Superadmin only. The validation and picker endpoints read the host filesystem, so they carry
the same capability check as the form itself.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Deployment, Project
from app.services.app_settings import StoredSettings
from app.services.deployments import (
    DEPLOYMENT_NAME_RULE,
    DeploymentForm,
    DeploymentFormError,
    RootCheck,
    check_root_path,
    create_deployment,
    update_deployment,
    validate_deployment,
)
from app.services.path_browser import BrowseView, browse_directory, browse_top
from app.services.paths import AllowedPaths, PathValidationError, load_allowed_paths
from app.services.permissions import Capability
from app.web.dependencies import (
    AuthContext,
    get_allowed_paths,
    get_db,
    get_stored_settings,
    require_capability,
)
from app.web.deployments import DeploymentContext, deployment_access
from app.web.htmx import hx_redirect
from app.web.project_detail import ProjectContext, project_access

router = APIRouter()

PAGE = "pages/deployment_form.html"
FORM_FRAGMENT = "fragments/deployment_form.html"
CHECKS_FRAGMENT = "fragments/path_checks.html"
PICKER_FRAGMENT = "fragments/path_picker.html"
PICKER_ERROR = "That folder cannot be opened."


@dataclass(frozen=True)
class FormTarget:
    """Where a deployment form posts, what its button says and where Cancel goes."""

    action: str
    submit_label: str
    cancel_href: str


def _create_target(project_id: int) -> FormTarget:
    """The create form of a project."""
    return FormTarget(
        action=f"/projects/{project_id}/deployments",
        submit_label="Create deployment",
        cancel_href=f"/projects/{project_id}",
    )


def _edit_target(deployment_id: int) -> FormTarget:
    """The edit form of a deployment."""
    return FormTarget(
        action=f"/deployments/{deployment_id}/edit",
        submit_label="Save deployment",
        cancel_href=f"/deployments/{deployment_id}/settings",
    )


async def posted_deployment_form(
    name: Annotated[str, Form()] = "",
    root: Annotated[str, Form()] = "",
    dist_missing_policy: Annotated[str, Form()] = "",
    drift_policy: Annotated[str, Form()] = "",
    missing_target_policy: Annotated[str, Form()] = "",
    file_mode: Annotated[str, Form()] = "",
    file_owner: Annotated[str, Form()] = "",
    file_group: Annotated[str, Form()] = "",
) -> DeploymentForm:
    """The deployment form as posted, a missing field reading as empty text."""
    return DeploymentForm(
        name=name,
        root=root,
        dist_missing_policy=dist_missing_policy,
        drift_policy=drift_policy,
        missing_target_policy=missing_target_policy,
        file_mode=file_mode,
        file_owner=file_owner,
        file_group=file_group,
    )


def _form_context(
    form: DeploymentForm,
    target: FormTarget,
    *,
    errors: dict[str, str] | None = None,
    root_check: RootCheck | None = None,
) -> dict[str, Any]:
    """What the form fragment needs: values, per field errors and the root path check lines."""
    return {
        "form": form,
        "errors": errors or {},
        "name_rule": DEPLOYMENT_NAME_RULE,
        "root_check": root_check,
        "action": target.action,
        "submit_label": target.submit_label,
        "cancel_href": target.cancel_href,
    }


def _refused(
    request: Request, form: DeploymentForm, target: FormTarget, error: DeploymentFormError
) -> Response:
    """The form again with the reason each refused value was refused (422)."""
    context = _form_context(form, target, errors=error.errors, root_check=error.root_check)
    return render(request, FORM_FRAGMENT, context, status_code=422)


def _picker_view(allowed: AllowedPaths) -> BrowseView:
    """The picker's starting list, from the allowed paths the request already loaded."""
    return browse_top(allowed)


def _page_context(
    allowed: AllowedPaths,
    *,
    title: str,
    project_id: int,
    project_name: str,
    form_context: dict[str, Any],
) -> dict[str, Any]:
    """The page around the form: heading, breadcrumb pieces and the picker's first list."""
    return {
        "title": title,
        "project_id": project_id,
        "project_name": project_name,
        "picker": _picker_view(allowed),
        **form_context,
    }


async def _initial_root_check(db: AsyncSession, root: str) -> RootCheck | None:
    """The root check lines for a prefilled path, None when there is nothing to check."""
    if not root:
        return None
    return check_root_path(await load_allowed_paths(db), root)


@router.get("/projects/{project_id:int}/deployments/new", include_in_schema=False)
async def new_deployment_page(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    allowed: Annotated[AllowedPaths, Depends(get_allowed_paths)],
    stored: Annotated[StoredSettings, Depends(get_stored_settings)],
) -> Response:
    """The deployment form, with the policies and file defaults of the General settings."""
    project = access.project
    context = _form_context(
        DeploymentForm.from_defaults(stored.general), _create_target(project.id)
    )
    return render(
        request,
        PAGE,
        _page_context(
            allowed,
            title="New deployment",
            project_id=project.id,
            project_name=project.name,
            form_context=context,
        ),
    )


@router.post("/projects/{project_id:int}/deployments", include_in_schema=False)
async def create_deployment_route(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    form: Annotated[DeploymentForm, Depends(posted_deployment_form)],
) -> Response:
    """Create a deployment. A refused value swaps the form again with the reasons (422)."""
    project = await db.get(Project, access.project.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    allowed = await load_allowed_paths(db)
    try:
        clean = await validate_deployment(db, allowed, project, form)
    except DeploymentFormError as error:
        return _refused(request, form, _create_target(project.id), error)
    deployment = await create_deployment(
        db, project, clean, actor=access.auth.user, ip_address=client_address(request)
    )
    return hx_redirect(f"/deployments/{deployment.id}/settings")


@router.get("/deployments/{deployment_id:int}/edit", include_in_schema=False)
async def edit_deployment_page(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    allowed: Annotated[AllowedPaths, Depends(get_allowed_paths)],
) -> Response:
    """The deployment form filled with the stored values."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    form = DeploymentForm.from_deployment(deployment)
    context = _form_context(
        form,
        _edit_target(deployment.id),
        root_check=await _initial_root_check(db, form.root),
    )
    return render(
        request,
        PAGE,
        _page_context(
            allowed,
            title=f"Edit {deployment.name}",
            project_id=access.deployment.project_id,
            project_name=access.deployment.project_name,
            form_context=context,
        ),
    )


@router.post("/deployments/{deployment_id:int}/edit", include_in_schema=False)
async def edit_deployment_route(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    form: Annotated[DeploymentForm, Depends(posted_deployment_form)],
) -> Response:
    """Save a deployment. A refused value swaps the form again with the reasons (422)."""
    deployment = await db.get(Deployment, access.deployment.id)
    project = await db.get(Project, access.deployment.project_id)
    if deployment is None or project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    allowed = await load_allowed_paths(db)
    try:
        clean = await validate_deployment(db, allowed, project, form, ignore_id=deployment.id)
    except DeploymentFormError as error:
        return _refused(request, form, _edit_target(deployment.id), error)
    await update_deployment(
        db, deployment, clean, actor=access.auth.user, ip_address=client_address(request)
    )
    return hx_redirect(f"/deployments/{deployment.id}/settings?saved=true")


@router.get("/paths/validate", include_in_schema=False)
async def validate_path(
    request: Request,
    _context: Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    root: str = "",
) -> Response:
    """The validation lines for a typed root path, swapped into the field's message list."""
    check = check_root_path(await load_allowed_paths(db), root)
    return render(request, CHECKS_FRAGMENT, {"root_check": check})


@router.get("/paths/browse", include_in_schema=False)
async def browse_paths(
    request: Request,
    _context: Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    path: str = "",
) -> Response:
    """The picker list: the allowed roots, or the folders inside one directory."""
    allowed = await load_allowed_paths(db)
    if not path.strip():
        return render(request, PICKER_FRAGMENT, {"picker": browse_top(allowed)})
    try:
        view = browse_directory(allowed, path)
    except PathValidationError:
        return render(
            request,
            PICKER_FRAGMENT,
            {"picker": browse_top(allowed), "picker_error": PICKER_ERROR},
            status_code=422,
        )
    return render(request, PICKER_FRAGMENT, {"picker": view})
