"""Deployment form routes: create, edit, root path validation and the path picker.

Superadmin only. The validation and picker endpoints read the host filesystem, so they carry
the same capability check as the form itself. A new deployment starts from a stack found on the
host (the routes of ``deployment_stacks``) or from a typed folder and ends in the create route
here, which checks every posted value again.
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Deployment, Project
from app.services.app_settings import StoredSettings
from app.services.compose_config import ComposeScanError
from app.services.compose_files import link_compose_files
from app.services.compose_project import COMPOSE_PROJECT_MESSAGE
from app.services.compose_scan import resolve_compose_files
from app.services.deployments import (
    DEPLOYMENT_NAME_RULE,
    CleanDeployment,
    DeploymentForm,
    DeploymentFormError,
    RootCheck,
    check_root_path,
    create_deployment,
    update_deployment,
    validate_deployment,
)
from app.services.docker_cli import DockerCli
from app.services.path_browser import BrowseView, browse_directory, browse_top
from app.services.paths import AllowedPaths, PathValidationError, load_allowed_paths
from app.services.permissions import Capability
from app.services.stack_discovery import projects_at_root
from app.web.dependencies import (
    AuthContext,
    get_allowed_paths,
    get_db,
    get_docker_cli,
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
SUGGESTION_FRAGMENT = "fragments/compose_project_suggestion.html"
PICKER_ERROR = "That folder cannot be opened."
COMPOSE_PROJECT_HINT = (
    "Optional. The name the stack's containers carry in Docker. " + COMPOSE_PROJECT_MESSAGE
)


@dataclass(frozen=True)
class FormTarget:
    """Where a deployment form posts, what its button says and where Cancel goes.

    ``suggest_href`` is the address of the running containers suggestion for the Compose project
    field, set on the edit form only.
    """

    action: str
    submit_label: str
    cancel_href: str
    suggest_href: str | None = None


@dataclass(frozen=True)
class StackFields:
    """What a stack picked on the New deployment page adds to the form.

    ``compose_files`` are the Compose files to link (relative to the root), ``import_next`` says
    whether to open the Compose import after the deployment is created, and ``project_locked``
    keeps the Compose project read only because it was read from Docker.
    """

    compose_files: tuple[str, ...] = ()
    import_next: bool = False
    project_locked: bool = False


def create_target(project_id: int) -> FormTarget:
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
        suggest_href=f"/deployments/{deployment_id}/edit/compose-project-suggestion",
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
    compose_project: Annotated[str, Form()] = "",
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
        compose_project=compose_project,
    )


async def posted_stack_fields(
    compose_file: Annotated[list[str] | None, Form()] = None,
    import_next: Annotated[bool, Form()] = False,
    project_locked: Annotated[bool, Form()] = False,
) -> StackFields:
    """The stack fields of the create form as posted: the Compose files in their order, and the
    import and read only flags (an unticked box posts nothing)."""
    return StackFields(
        compose_files=tuple(compose_file or ()),
        import_next=import_next,
        project_locked=project_locked,
    )


def form_context(
    form: DeploymentForm,
    target: FormTarget,
    *,
    errors: dict[str, str] | None = None,
    root_check: RootCheck | None = None,
    stack: StackFields | None = None,
) -> dict[str, Any]:
    """What the form fragment needs: values, per field errors, the root path check lines and the
    fields of a picked stack."""
    return {
        "form": form,
        "errors": errors or {},
        "name_rule": DEPLOYMENT_NAME_RULE,
        "project_rule": COMPOSE_PROJECT_HINT,
        "root_check": root_check,
        "stack": stack or StackFields(),
        "suggest_href": None if form.compose_project.strip() else target.suggest_href,
        "action": target.action,
        "submit_label": target.submit_label,
        "cancel_href": target.cancel_href,
    }


def _refused(
    request: Request,
    form: DeploymentForm,
    target: FormTarget,
    error: DeploymentFormError,
    stack: StackFields | None = None,
) -> Response:
    """The form again with the reason each refused value was refused (422)."""
    context = form_context(
        form, target, errors=error.errors, root_check=error.root_check, stack=stack
    )
    return render(request, FORM_FRAGMENT, context, status_code=422)


def _picker_view(allowed: AllowedPaths) -> BrowseView:
    """The picker's starting list, from the allowed paths the request already loaded."""
    return browse_top(allowed)


def page_context(
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
    source: str = "stacks",
) -> Response:
    """The new deployment page: the stacks found on the host (loaded after the page) or, with
    ``source=folder``, the form with a typed folder, with the policies and file defaults of the
    General settings."""
    project = access.project
    context = form_context(DeploymentForm.from_defaults(stored.general), create_target(project.id))
    base = f"/projects/{project.id}/deployments/new"
    return render(
        request,
        PAGE,
        {
            **page_context(
                allowed,
                title="New deployment",
                project_id=project.id,
                project_name=project.name,
                form_context=context,
            ),
            "new_source": "folder" if source == "folder" else "stacks",
            "new_href": base,
            "stacks_href": f"{base}/stacks",
        },
    )


async def _validated(
    db: AsyncSession,
    allowed: AllowedPaths,
    project: Project,
    form: DeploymentForm,
    compose_files: tuple[str, ...],
) -> CleanDeployment:
    """The clean values of a new deployment, the Compose files checked against its real root.

    Raises ``DeploymentFormError`` with every problem found; the files are not checked while
    the root itself is refused.
    """
    errors: dict[str, str] = {}
    root_check: RootCheck | None = None
    clean: CleanDeployment | None = None
    try:
        clean = await validate_deployment(db, allowed, project, form)
    except DeploymentFormError as error:
        errors, root_check = dict(error.errors), error.root_check
    if clean is not None:
        root_check = clean.root_check
        if compose_files:
            try:
                await asyncio.to_thread(
                    resolve_compose_files, allowed, clean.root_path, list(compose_files)
                )
            except ComposeScanError as refused:
                errors["compose_files"] = str(refused)
    if errors or clean is None:
        raise DeploymentFormError(errors, root_check)
    return clean


def _landing(deployment: Deployment, stack: StackFields, linked: bool) -> str:
    """Where a created deployment goes: the Compose import with its files ticked when asked
    for, the Files tab with the saved toast, or Settings when the files could not be linked."""
    if stack.compose_files and not linked:
        return f"/deployments/{deployment.id}/settings"
    if stack.compose_files and stack.import_next:
        query = urlencode([("compose_file", path) for path in deployment.compose_files or ()])
        return f"/deployments/{deployment.id}/import-compose?{query}"
    return f"/deployments/{deployment.id}/files?saved=true"


@router.post("/projects/{project_id:int}/deployments", include_in_schema=False)
async def create_deployment_route(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    form: Annotated[DeploymentForm, Depends(posted_deployment_form)],
    stack: Annotated[StackFields, Depends(posted_stack_fields)],
) -> Response:
    """Create a deployment and link its Compose files. A refused value swaps the form again with
    the reasons (422); nothing is created then."""
    project = await db.get(Project, access.project.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    allowed = await load_allowed_paths(db)
    try:
        clean = await _validated(db, allowed, project, form, stack.compose_files)
    except DeploymentFormError as error:
        return _refused(request, form, create_target(project.id), error, stack)
    ip_address = client_address(request)
    deployment = await create_deployment(
        db, project, clean, actor=access.auth.user, ip_address=ip_address
    )
    linked = not stack.compose_files
    if stack.compose_files:
        try:
            await link_compose_files(
                db, deployment, stack.compose_files, actor=access.auth.user, ip_address=ip_address
            )
            linked = True
        except ComposeScanError:
            # A file changed between the check and the link; the deployment exists and the
            # files can be linked from its Settings tab.
            linked = False
    return hx_redirect(_landing(deployment, stack, linked))


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
    context = form_context(
        form,
        _edit_target(deployment.id),
        root_check=await _initial_root_check(db, form.root),
    )
    return render(
        request,
        PAGE,
        page_context(
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


@router.get(
    "/deployments/{deployment_id:int}/edit/compose-project-suggestion", include_in_schema=False
)
async def compose_project_suggestion(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
) -> Response:
    """The offer to fill the Compose project from Docker: the one project whose folder is the
    root of the deployment. Nothing is offered when there are none or several, when the
    deployment has a project already, or when Docker cannot be asked."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    names: tuple[str, ...] = ()
    if not deployment.compose_project:
        names = await projects_at_root(cli, await load_allowed_paths(db), deployment.root_path)
    return render(
        request, SUGGESTION_FRAGMENT, {"suggested": names[0] if len(names) == 1 else None}
    )


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
