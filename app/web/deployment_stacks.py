"""The stacks found on the host for the New deployment page, and the form a picked one fills.

Superadmin only, like the form itself. Discovery reads Docker and the allowed paths, which can
take seconds, so the page loads the list as a fragment after it is shown. A pick (Use) carries the
folder, the project name and the Compose files of the stack in the address; the form it returns is
only a starting point, since the create route checks every value again.
"""

import asyncio
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.services.app_settings import StoredSettings
from app.services.compose_config import ComposeScanError
from app.services.compose_project import is_compose_project_name
from app.services.compose_scan import MAX_COMPOSE_FILES, resolve_compose_files
from app.services.deployments import DeploymentForm, check_root_path, suggest_deployment_name
from app.services.docker_cli import DockerCli
from app.services.paths import load_allowed_paths
from app.services.permissions import Capability
from app.services.stack_discovery import DetectedStack, discover_stacks
from app.web.dependencies import get_db, get_docker_cli, get_stored_settings
from app.web.deployment_form import FORM_FRAGMENT, StackFields, create_target, form_context
from app.web.project_detail import ProjectContext, project_access

router = APIRouter()

STACKS_FRAGMENT = "fragments/detected_stacks.html"
LOADING_FRAGMENT = "fragments/detected_stacks_loading.html"
BASE = "/projects/{project_id:int}/deployments/new"


@dataclass(frozen=True)
class StackRow:
    """A stack as the list shows it and the address that fills the form from it (None when the
    stack cannot be used)."""

    stack: DetectedStack
    use_href: str | None


def use_address(project_id: int, stack: DetectedStack) -> str:
    """The address of the form a stack fills: its folder, project and Compose files."""
    query = [("folder", stack.folder)]
    if stack.project:
        query.append(("project", stack.project))
    query.extend(("compose_file", path) for path in stack.compose_files)
    return f"/projects/{project_id}/deployments/new/use?{urlencode(query)}"


@router.get(f"{BASE}/stacks/loading", include_in_schema=False)
async def stacks_loading(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
) -> Response:
    """The loading state of the list, which asks for the list itself as soon as it is shown."""
    stacks_href = f"/projects/{access.project.id}/deployments/new/stacks"
    return render(request, LOADING_FRAGMENT, {"stacks_href": stacks_href})


@router.get(f"{BASE}/stacks", include_in_schema=False)
async def detected_stacks(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
) -> Response:
    """The stacks of the host: from Docker when it is usable and from the allowed paths."""
    project_id = access.project.id
    found = await discover_stacks(db, cli, await load_allowed_paths(db))
    rows = tuple(
        StackRow(
            stack, use_address(project_id, stack) if stack.usable and not stack.used_by else None
        )
        for stack in found.stacks
    )
    return render(
        request,
        STACKS_FRAGMENT,
        {
            "rows": rows,
            "docker_reason": found.docker_reason,
            "scan_href": f"/projects/{project_id}/deployments/new/stacks/loading",
            "folder_href": f"/projects/{project_id}/deployments/new?source=folder",
        },
    )


@router.get(f"{BASE}/use", include_in_schema=False)
async def use_stack(
    request: Request,
    access: Annotated[ProjectContext, Depends(project_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    stored: Annotated[StoredSettings, Depends(get_stored_settings)],
    folder: str = "",
    project: str = "",
    compose_file: Annotated[list[str] | None, Query()] = None,
) -> Response:
    """The deployment form filled from a stack: the name made from its project or folder, the
    folder, the Compose files and the project (read only when Docker named it)."""
    allowed = await load_allowed_paths(db)
    root_check = check_root_path(allowed, folder)
    project_name = project.strip()
    files = tuple(compose_file or ())[: MAX_COMPOSE_FILES + 1]
    errors: dict[str, str] = {}
    if files and root_check.real_path is not None:
        try:
            await asyncio.to_thread(
                resolve_compose_files, allowed, str(root_check.real_path), list(files)
            )
        except ComposeScanError as refused:
            errors["compose_files"] = str(refused)
    form = replace(
        DeploymentForm.from_defaults(stored.general),
        name=suggest_deployment_name(project_name or PurePosixPath(folder.rstrip("/")).name),
        root=folder,
        compose_project=project_name,
    )
    stack = StackFields(
        compose_files=files,
        import_next=bool(files),
        project_locked=bool(project_name) and is_compose_project_name(project_name),
    )
    context = form_context(
        form, create_target(access.project.id), errors=errors, root_check=root_check, stack=stack
    )
    return render(request, FORM_FRAGMENT, context)
