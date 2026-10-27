"""The Compose files of a deployment: the read only view of one, and linking them.

Viewing a linked Compose file needs ``Capability.VIEW_COMPOSE_FILES`` over the deployment and is
audited; linking, changing or unlinking them is the superadmin's (manage structure). Both answer
inside a dialog of the Compose files panel. The paths are checked by the services against the
deployment folder and the allowed paths of now; a view names the file by its position in the
linked list, never by a posted path.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Deployment
from app.services.compose_config import ComposeScanError
from app.services.compose_files import link_compose_files, read_compose_file
from app.services.compose_scan import MAX_COMPOSE_FILES, ComposeFileOffer, find_compose_files
from app.services.permissions import Capability
from app.web.dependencies import get_db
from app.web.deployments import DeploymentContext, deployment_access
from app.web.htmx import hx_redirect

router = APIRouter()

BASE = "/deployments/{deployment_id:int}/compose-files"
VIEW_FRAGMENT = "fragments/compose_file_view.html"
LINK_FRAGMENT = "fragments/compose_link.html"
MAX_TYPED_PATH = 500


async def _stored(db: AsyncSession, access: DeploymentContext) -> Deployment:
    """The deployment row of a request, or 404."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    return deployment


@router.get(f"{BASE}/{{index:int}}", include_in_schema=False)
async def view_compose_file(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.VIEW_COMPOSE_FILES))],
    db: Annotated[AsyncSession, Depends(get_db)],
    index: int,
) -> Response:
    """The current text of one linked Compose file, or why it cannot be shown (422)."""
    deployment = await _stored(db, access)
    linked = deployment.compose_files or []
    path = linked[index] if 0 <= index < len(linked) else "Compose file"
    try:
        shown = await read_compose_file(
            db,
            deployment,
            index,
            actor=access.auth.user,
            ip_address=client_address(request),
        )
    except ComposeScanError as error:
        return render(
            request,
            VIEW_FRAGMENT,
            {"path": path, "text": None, "error": str(error)},
            status_code=422,
        )
    return render(request, VIEW_FRAGMENT, {"path": shown.path, "text": shown.text, "error": None})


def _link_context(
    access: DeploymentContext,
    offers: tuple[ComposeFileOffer, ...],
    chosen: list[str],
    typed: str,
    error: str | None,
) -> dict[str, object]:
    """What the link dialog shows."""
    return {
        "action": f"/deployments/{access.deployment.id}/compose-files/link",
        "deployment_name": access.deployment.name,
        "offers": offers,
        "chosen": set(chosen),
        "typed": typed,
        "error": error,
    }


@router.get(f"{BASE}/link", include_in_schema=False)
async def link_dialog(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The link dialog: the Compose files found, the linked ones ticked, and a linked file the
    folder scan does not find in the path field."""
    deployment = await _stored(db, access)
    linked = list(deployment.compose_files or ())
    error: str | None = None
    offers: tuple[ComposeFileOffer, ...] = ()
    try:
        offers = await find_compose_files(db, deployment)
    except ComposeScanError as refused:
        error = str(refused)
    found = {offer.path for offer in offers}
    typed = next((path for path in linked if path not in found), "")
    return render(request, LINK_FRAGMENT, _link_context(access, offers, linked, typed, error))


@router.post(f"{BASE}/link", include_in_schema=False)
async def link_files(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    compose_file: Annotated[list[str] | None, Form()] = None,
    extra_path: Annotated[str, Form()] = "",
) -> Response:
    """Link the ticked Compose files, in the order of the folder, and the typed one last; none
    unlinks them. Success leaves for the deployment page with the counts; a refusal draws the
    dialog again with the reason (422) and changes nothing."""
    deployment = await _stored(db, access)
    posted = [path.strip() for path in compose_file or []][:MAX_COMPOSE_FILES]
    typed = extra_path.strip()[:MAX_TYPED_PATH]
    offers: tuple[ComposeFileOffer, ...] = ()
    try:
        offers = await find_compose_files(db, deployment)
        ordered = [offer.path for offer in offers if offer.path in posted]
        if typed and typed not in ordered:
            ordered.append(typed)
        dropped = await link_compose_files(
            db,
            deployment,
            ordered,
            actor=access.auth.user,
            ip_address=client_address(request),
        )
    except ComposeScanError as refused:
        context = _link_context(access, offers, posted, typed, str(refused))
        return render(request, LINK_FRAGMENT, context, status_code=422)
    linked = len(deployment.compose_files or ())
    return hx_redirect(
        f"/deployments/{deployment.id}/settings?compose_linked={linked},{len(dropped.removed)}"
    )
