"""Dist comparison routes: the report of one environment and the overview of a deployment.

Opening a report, running it again and opening the overview each run a check, and every check is
audited. Report screens hold key names and counts only, never a value. Adding the missing keys
goes through the editor's save, so it needs the edit capability and makes an audited new version.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.services.dist import DistError
from app.services.dist_check import (
    DistCheck,
    add_missing_keys,
    run_deployment_dist_checks,
    run_dist_check,
)
from app.services.editor import EditorError
from app.services.permissions import CAPABILITY_GROUPS, Capability
from app.web.dependencies import get_crypto, get_db, get_notifier
from app.web.deployments import DeploymentContext, deployment_access
from app.web.editor import EnvironmentContext, environment_access

router = APIRouter()

PAGE = "pages/dist_report.html"
REFRESH = "fragments/dist_refresh.html"
OVERVIEW = "pages/deployment_dist.html"
MAX_NUMBER_DIGITS = 9


def _plain_number(value: str) -> str:
    """The value when it is a plain number, else empty."""
    return value if value.isdecimal() and len(value) <= MAX_NUMBER_DIGITS else ""


async def _check(db: AsyncSession, request: Request, access: EnvironmentContext) -> DistCheck:
    """Run and audit a check for the environment; an environment that vanished answers 404."""
    try:
        return await run_dist_check(
            db,
            get_crypto(request),
            access.env.id,
            actor=access.auth.user,
            ip_address=client_address(request),
        )
    except DistError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "environment not found") from error


def _context(access: EnvironmentContext, check: DistCheck, **extra: Any) -> dict[str, Any]:
    """What the page and its fragments need: the environment, the check and the edit right."""
    return {
        "env": access.env,
        "check": check,
        "can_edit": access.env.can_edit,
        "error": None,
        "is_viewer": access.scope.group not in CAPABILITY_GROUPS[Capability.EDIT_VALUES],
        **extra,
    }


@router.get("/environments/{environment_id:int}/dist", include_in_schema=False)
async def dist_page(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.DEPLOY))],
    db: Annotated[AsyncSession, Depends(get_db)],
    added: str = "",
    version: str = "",
) -> Response:
    """The dist report of an environment, from a check run now.

    ``added`` (a key count) and ``version`` show the toast after adding the missing keys
    redirected here.
    """
    check = await _check(db, request, access)
    return render(
        request,
        PAGE,
        _context(
            access,
            check,
            tab="dist",
            added=_plain_number(added),
            added_version=_plain_number(version),
        ),
    )


@router.post("/environments/{environment_id:int}/dist-check", include_in_schema=False)
async def dist_check_again(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.DEPLOY))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Run the check again: the report fragment plus the refreshed page header."""
    check = await _check(db, request, access)
    return render(request, REFRESH, _context(access, check))


@router.post("/environments/{environment_id:int}/add-missing", include_in_schema=False)
async def add_missing(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.DEPLOY))],
    db: Annotated[AsyncSession, Depends(get_db)],
    base: str = "",
) -> Response:
    """Save the keys missing from the environment, with empty values, as a new version.

    ``base`` is the version the report was built from. Success reloads the report; a refusal
    answers a fresh report with the reason (422).
    """
    if not access.env.can_edit:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
    try:
        added = await add_missing_keys(
            db,
            get_crypto(request),
            access.env,
            actor=access.auth.user,
            ip_address=client_address(request),
            base_number=int(_plain_number(base) or 0),
            notifier=get_notifier(request),
        )
    except EditorError as error:
        check = await _check(db, request, access)
        return render(request, REFRESH, _context(access, check, error=str(error)), status_code=422)
    return Response(
        status_code=200,
        headers={
            "HX-Redirect": (
                f"/environments/{access.env.id}/dist"
                f"?added={len(added.keys)}&version={added.version.number}"
            ),
            "Cache-Control": "no-store",
            "Vary": "HX-Request",
        },
    )


@router.get("/deployments/{deployment_id:int}/dist", include_in_schema=False)
async def deployment_dist_page(
    request: Request,
    access: Annotated[DeploymentContext, Depends(deployment_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Dist checks of every file of the deployment in the caller's scope, run now.

    Nothing is read while the deployment lies outside the allowed paths.
    """
    if not access.deployment.can_check_dist:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
    checks: tuple[DistCheck, ...] = ()
    if not access.deployment.outside_allowed:
        checks = await run_deployment_dist_checks(
            db,
            get_crypto(request),
            access.scope,
            access.deployment.id,
            actor=access.auth.user,
            ip_address=client_address(request),
        )
    return render(
        request,
        OVERVIEW,
        {
            "deployment": access.deployment,
            "checks": checks,
        },
    )
