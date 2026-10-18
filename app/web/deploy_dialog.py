"""The deploy dialog: the pre-flight dry run and the deploy, both answered as dialog fragments.

Both routes are POSTs behind CSRF: even the pre-flight records a dry run and an audit entry. A
service outside the caller's scope answers 404 and one the caller may not deploy answers 403;
the chosen environments are checked against what the caller may deploy on the server whatever
the page offered. A deploy with the force flag is only run when the request carries the
confirmation the force step posts; without it the answer is that step.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from app.core.templating import render
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_dialog import (
    build_result,
    run_preflight,
    selected_names,
)
from app.services.deploy_types import DeployRequest, DeployRequestError
from app.services.docker_cli import DockerCli
from app.services.hook_settings import MAX_TIMEOUT_SECONDS
from app.services.permissions import Capability, permitted_environment_ids
from app.web.dependencies import get_db, get_deploy_engine, get_docker_cli
from app.web.service_detail import ServiceContext, service_access

router = APIRouter()

PREFLIGHT = "fragments/deploy_preflight.html"
CONFIRM = "fragments/deploy_confirm.html"
RESULT = "fragments/deploy_result.html"
PROBLEM = "fragments/deploy_problem.html"
SELECT_NOTHING = "Select at least one environment to deploy."
# A deploy waits for the post-deploy command, which may run up to its largest allowed timeout;
# the margin covers the file work around it. Without this the 60 second default of htmx would
# cut the request while the deploy still runs on the server.
DEPLOY_TIMEOUT_MS = (MAX_TIMEOUT_SECONDS + 120) * 1000
MAX_NUMBER_DIGITS = 9
VERSION_FIELD_PREFIX = "version_"


@dataclass(frozen=True)
class DialogForm:
    """What the dialog posted: the chosen environments, pinned versions and the force flags.

    ``selected`` is None when the request names no choice, so the default one applies.
    """

    selected: set[int] | None
    versions: dict[int, int]
    force: bool
    confirmed: bool


def _number(value: object) -> int | None:
    """The value as a positive whole number, None for anything else."""
    text = str(value)
    if text.isdecimal() and len(text) <= MAX_NUMBER_DIGITS and int(text) > 0:
        return int(text)
    return None


def parse_dialog_form(form: FormData) -> DialogForm:
    """Read the posted fields; anything that is not a plain number is ignored."""
    chosen = {
        number for value in form.getlist("environment") if (number := _number(value)) is not None
    }
    explicit = form.get("selection") == "explicit"
    versions: dict[int, int] = {}
    for key, value in form.multi_items():
        if not key.startswith(VERSION_FIELD_PREFIX):
            continue
        environment = _number(key[len(VERSION_FIELD_PREFIX) :])
        version = _number(value)
        if environment is not None and version is not None:
            versions[environment] = version
    return DialogForm(
        selected=chosen if chosen or explicit else None,
        versions=versions,
        force=form.get("force") is not None,
        confirmed=form.get("confirm_force") == "1",
    )


async def deploy_access(
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServiceContext:
    """The service for a caller who may deploy at least one of its environments.

    A service outside the scope answers 404 (through ``service_access``); one in scope where the
    caller may deploy nothing, a viewer for instance, answers 403.
    """
    permitted = await permitted_environment_ids(
        db, access.scope, Capability.DEPLOY, service_id=access.service.id
    )
    if not permitted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
    return access


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _selection_context(form: DialogForm) -> dict[str, object]:
    """The fields a fragment carries forward to the next request."""
    return {
        "selected_ids": sorted(form.selected or ()),
        "versions": form.versions,
        "force": form.force,
        "deploy_timeout_ms": DEPLOY_TIMEOUT_MS,
    }


@router.post("/services/{service_id:int}/deploy/preflight", include_in_schema=False)
async def preflight(
    request: Request,
    access: Annotated[ServiceContext, Depends(deploy_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    engine: Annotated[DeployEngine, Depends(get_deploy_engine)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
) -> Response:
    """Step one: a dry run of every environment the caller may deploy, with the policy outcome."""
    form = parse_dialog_form(await request.form())
    actor = AuditActor.for_user(access.auth.user, _client_ip(request))
    try:
        result = await run_preflight(
            db,
            engine,
            cli,
            access.scope,
            actor,
            access.service,
            selected_ids=form.selected,
            versions=form.versions,
            force=form.force,
        )
    except DeployRequestError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    return render(
        request,
        PREFLIGHT,
        {"service": access.service, "preflight": result, "deploy_timeout_ms": DEPLOY_TIMEOUT_MS},
    )


@router.post("/services/{service_id:int}/deploy", include_in_schema=False)
async def deploy(
    request: Request,
    access: Annotated[ServiceContext, Depends(deploy_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    engine: Annotated[DeployEngine, Depends(get_deploy_engine)],
) -> Response:
    """Step two: deploy the chosen environments and answer with the result.

    Nothing is deployed without a choice, and a forced deploy waits for its confirmation. A
    recorded run sends ``deploy-finished`` so the panels behind the dialog refresh.
    """
    form = parse_dialog_form(await request.form())
    context: dict[str, object] = {"service": access.service, **_selection_context(form)}
    if not form.selected:
        return render(request, PROBLEM, {**context, "message": SELECT_NOTHING}, status_code=422)
    try:
        names = await selected_names(db, access.scope, access.service, form.selected)
        if form.force and not form.confirmed:
            return render(request, CONFIRM, {**context, "names": names})
        outcome = await engine.run(
            db,
            access.scope,
            AuditActor.for_user(access.auth.user, _client_ip(request)),
            DeployRequest(
                service_id=access.service.id,
                environment_ids=tuple(sorted(form.selected)),
                versions=form.versions,
                force=form.force,
            ),
        )
    except DeployRequestError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    view = await build_result(db, access.service, outcome)
    response = render(request, RESULT, {**context, "result": view})
    if outcome.run_id is not None:
        response.headers["HX-Trigger"] = "deploy-finished"
    return response
