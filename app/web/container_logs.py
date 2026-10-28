"""The Logs tab of a deployment: the page, the poll that appends the newest lines and the download.

All three need ``Capability.VIEW_CONTAINER_LOGS`` on the deployment, so a caller who reaches it
through a service or a file is refused (403) and one who cannot see it gets 404.

The page shows the services and the filters and asks for the first lines as soon as it is shown.
``lines`` answers every request for lines. Without a cursor it is the first read (the last lines
of each container, written to the audit log once per user, deployment and service filter in ten
minutes, and the box is refilled); with one it is a poll that gives the lines after it. The answer
is always the poller (the footer that carries the applied filter and the cursor and, while
following, asks again after two seconds), with the lines appended to the box out of band. A read
that fails is a notice in the footer, never an error page, and the box keeps what it shows.
``download`` answers the same lines as a text file, always audited.
Nothing is stored, and no line is logged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.templating import render
from app.models import AuditResult, Deployment
from app.services.audit import AuditActor, record_actor_audit
from app.services.container_logs import (
    DEFAULT_TAIL,
    LOGS_VIEW_ACTION,
    MAX_FILTER_CHARACTERS,
    SERVICE_NAME,
    TAIL_CHOICES,
    LogRead,
    LogsUnavailableError,
    format_ns,
    opened_recently,
    read_logs,
    timestamp_ns,
)
from app.services.docker_cli import DockerCli
from app.services.docker_query import DockerQueryError
from app.services.permissions import Capability
from app.services.stack_runtime import RuntimeState, load_stack_runtime
from app.web.dependencies import get_clock, get_crypto, get_db, get_docker_cli
from app.web.deployments import DeploymentContext, deployment_access, page_context

router = APIRouter()

PAGE = "pages/deployment_logs.html"
LINES_FRAGMENT = "fragments/log_lines.html"
READ_FAILED_PREFIX = "The logs could not be read: "
Viewer = Annotated[DeploymentContext, Depends(deployment_access(Capability.VIEW_CONTAINER_LOGS))]


@dataclass(frozen=True)
class LogParams:
    """The applied filter: the service (None for all), the text that lines must hold and how many
    lines the first read shows."""

    service: str | None
    query: str
    tail: int

    @property
    def query_string(self) -> str:
        """The filter as the query of an address."""
        return urlencode({"service": self.service or "", "q": self.query, "tail": self.tail})


def log_params(service: str, query: str, tail: str) -> LogParams:
    """The filter of a request. A service name that cannot be one is a 404, a text is cut to its
    limit and a line count that is not a choice becomes the default."""
    name = service.strip()
    if name and SERVICE_NAME.fullmatch(name) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    count = int(tail) if tail.isdigit() else DEFAULT_TAIL
    return LogParams(
        service=name or None,
        query=query.strip()[:MAX_FILTER_CHARACTERS],
        tail=count if count in TAIL_CHOICES else DEFAULT_TAIL,
    )


def log_cursor(cursor: str) -> int | None:
    """The moment a cursor names, None for no cursor; a cursor that is not a stamp is a 400."""
    if not cursor:
        return None
    moment = timestamp_ns(cursor)
    if moment is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cursor not valid")
    return moment


async def _stored(db: AsyncSession, access: DeploymentContext) -> Deployment:
    """The deployment row of a request, or 404."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    return deployment


async def _audit_view(
    db: AsyncSession,
    request: Request,
    access: DeploymentContext,
    deployment: Deployment,
    params: LogParams,
    *,
    download: bool,
    now: datetime,
) -> None:
    """Record that the logs were opened: the service filter, the line count and whether it was a
    download. The lines and the text filter are not recorded."""
    await record_actor_audit(
        db,
        AuditActor.for_user(access.auth.user, client_address(request)),
        action=LOGS_VIEW_ACTION,
        result=AuditResult.SUCCESS,
        target_type="deployment",
        target_id=deployment.id,
        target_name=deployment.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        details={"service": params.service, "tail": params.tail, "download": download},
        created_at=now,
    )
    await db.commit()


@router.get("/deployments/{deployment_id:int}/logs", include_in_schema=False)
async def logs_tab(
    request: Request,
    access: Viewer,
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    service: str = "",
    q: str = "",
    tail: str = "",
) -> Response:
    """The Logs tab: the services as filters and the box, which asks for its lines when shown.

    The services are the ones that have a container now. A project that is not set, Docker that
    cannot be used and a project with no container are states of the page.
    """
    deployment = await _stored(db, access)
    runtime = await load_stack_runtime(db, cli, deployment)
    services = [item.name for item in runtime.services if item.containers]
    params = log_params(service, q, tail)
    if params.service not in services:
        params = LogParams(None, params.query, params.tail)
    return render(
        request,
        PAGE,
        page_context(
            access,
            "logs",
            state=runtime.state.value,
            ready=runtime.state is RuntimeState.READY,
            reason=runtime.reason,
            project=runtime.project,
            services=services,
            params=params,
            tail_choices=TAIL_CHOICES,
        ),
    )


@router.get("/deployments/{deployment_id:int}/logs/lines", include_in_schema=False)
async def log_lines(
    request: Request,
    access: Viewer,
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    service: str = "",
    q: str = "",
    tail: str = "",
    cursor: str = "",
    follow: str = "1",
    toggle: str = "",
) -> Response:
    """The poller with the lines after the cursor appended to the box (the first read refills
    it). The first read that works is audited unless the same user opened the same service
    filter of this deployment in the last ten minutes, so a chip, a pause in typing or a change
    of line count does not write an entry of its own. A read that fails is a notice."""
    params = log_params(service, q, tail)
    moment = log_cursor(cursor)
    deployment = await _stored(db, access)
    notice = None
    read = LogRead((), moment)
    try:
        read = await read_logs(
            db,
            crypto,
            cli,
            deployment,
            tail=params.tail,
            cursor=moment,
            service=params.service,
            query=params.query,
        )
    except (LogsUnavailableError, DockerQueryError) as failed:
        notice = READ_FAILED_PREFIX + failed.message
    first = moment is None
    if first and notice is None:
        now = clock()
        user_id = access.auth.user.id
        if not await opened_recently(db, user_id, deployment.id, params.service, now):
            await _audit_view(db, request, access, deployment, params, download=False, now=now)
    return render(
        request,
        LINES_FRAGMENT,
        {
            "deployment": access.deployment,
            "params": params,
            "lines": read.lines,
            "cursor": format_ns(read.cursor) if read.cursor is not None else "",
            "omitted": read.omitted,
            "following": follow != "0",
            "first": first,
            "toggle": bool(toggle),
            "notice": notice,
        },
    )


@router.get("/deployments/{deployment_id:int}/logs/download", include_in_schema=False)
async def log_download(
    request: Request,
    access: Viewer,
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    service: str = "",
    q: str = "",
    tail: str = "",
) -> Response:
    """The lines of the applied filter as a text file, masked like the box, always audited. A
    read that fails is a 409 with the reason as text."""
    params = log_params(service, q, tail)
    deployment = await _stored(db, access)
    headers = {"Cache-Control": "no-store", "Vary": "HX-Request"}
    try:
        read = await read_logs(
            db,
            crypto,
            cli,
            deployment,
            tail=params.tail,
            service=params.service,
            query=params.query,
        )
    except (LogsUnavailableError, DockerQueryError) as failed:
        return Response(
            READ_FAILED_PREFIX + failed.message,
            status_code=status.HTTP_409_CONFLICT,
            media_type="text/plain",
            headers=headers,
        )
    await _audit_view(db, request, access, deployment, params, download=True, now=clock())
    body = "".join(f"{line.stamp} {line.service} {line.plain}\n" for line in read.lines)
    suffix = f"-{params.service}" if params.service else ""
    name = f"deployment-{deployment.id}-logs{suffix}.txt"
    headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return Response(body, media_type="text/plain; charset=utf-8", headers=headers)
