"""Start, stop, restart and recreate a service's container from the Overview.

``GET`` answers the confirmation that loads into the dialog of the Overview, with the command
that will run (or the reason it cannot); ``POST`` runs it and answers the toast. Both need
``Capability.CONTROL_CONTAINERS`` on the deployment, so a caller who reaches it through a service
or a file is refused (403) and one who cannot see it gets 404. An action or a service name the
deployment does not have is a 404 too. A refusal that comes from the state of the host (no Docker,
a deploy running, Compose files that do not resolve) is a 409 with the reason as the toast, and a
command that ran and failed is a 200 with the same kind of toast; both close the dialog and ask
the Overview to read the containers again.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.templating import render
from app.models import Deployment
from app.models.types import utc_now
from app.services.audit import AuditActor
from app.services.container_actions import (
    CONTROL_TIMEOUT_MS,
    ActionRefusedError,
    ActionStatus,
    ContainerAction,
    UnknownServiceError,
    prepare_container_action,
    run_container_action,
)
from app.services.docker_cli import DockerCli
from app.services.permissions import Capability
from app.web.dependencies import get_crypto, get_db, get_docker_cli, get_lock_dir
from app.web.deployments import DeploymentContext, deployment_access
from app.web.htmx import hx_events

router = APIRouter()

BASE = "/deployments/{deployment_id:int}/containers/{service}/{action}"
DIALOG_FRAGMENT = "fragments/container_action.html"
RESULT_FRAGMENT = "fragments/container_result.html"
Control = Annotated[DeploymentContext, Depends(deployment_access(Capability.CONTROL_CONTAINERS))]


def _action_of(name: str) -> ContainerAction:
    """The action a path names, or 404."""
    try:
        return ContainerAction(name)
    except ValueError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found") from error


async def _stored(db: AsyncSession, access: DeploymentContext) -> Deployment:
    """The deployment row of a request, or 404."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    return deployment


@router.get(BASE, include_in_schema=False)
async def action_dialog(
    request: Request,
    access: Control,
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    service: str,
    action: str,
) -> Response:
    """The confirmation of an action: its effect, the command, or why it cannot run now."""
    chosen = _action_of(action)
    deployment = await _stored(db, access)
    plan = None
    refusal = None
    try:
        plan = await prepare_container_action(db, cli, deployment, chosen, service)
    except UnknownServiceError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found") from error
    except ActionRefusedError as refused:
        refusal = refused.message
    return render(
        request,
        DIALOG_FRAGMENT,
        {
            "deployment": access.deployment,
            "action": chosen,
            "service": service,
            "plan": plan,
            "refusal": refusal,
            "now": utc_now(),
            "control_timeout_ms": CONTROL_TIMEOUT_MS,
        },
    )


@router.post(BASE, include_in_schema=False)
async def run_action(
    request: Request,
    access: Control,
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    lock_dir: Annotated[Path, Depends(get_lock_dir)],
    service: str,
    action: str,
) -> Response:
    """Run the action and answer the toast that tells how it ended."""
    chosen = _action_of(action)
    deployment = await _stored(db, access)
    try:
        outcome = await run_container_action(
            db,
            crypto,
            cli,
            lock_dir,
            deployment,
            chosen,
            service,
            actor=AuditActor.for_user(access.auth.user, client_address(request)),
        )
    except UnknownServiceError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found") from error
    response = render(
        request,
        RESULT_FRAGMENT,
        {"outcome": outcome},
        status_code=status.HTTP_409_CONFLICT
        if outcome.status is ActionStatus.REFUSED
        else status.HTTP_200_OK,
    )
    response.headers["HX-Trigger"] = hx_events("close-dialogs", "containers-changed")
    return response
