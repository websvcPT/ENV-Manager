"""Drift view routes: the masked three-way comparison, the audited reveal and the two actions.

A render that is not an answer to a reveal never holds a value. Revealing is a POST that is
audited before the values leave, answers only the values and is never cached. Importing the target
as a version needs the edit capability and overwriting it (a forced deploy of that file) the deploy
capability; viewers see the masked comparison only and every action is enforced on the server.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from app.core.templating import render
from app.models import PostDeployStatus
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_types import DeployRequestError
from app.services.drift import (
    REVEAL_COLUMNS,
    DriftChangedError,
    DriftError,
    OverwriteResult,
    import_target,
    load_drift_view,
    overwrite_target,
    reveal_drift,
)
from app.services.permissions import Capability
from app.web.dependencies import get_crypto, get_db, get_deploy_engine
from app.web.deploy_dialog import DEPLOY_TIMEOUT_MS
from app.web.editor import EnvironmentContext, environment_access

router = APIRouter()

PAGE = "pages/drift.html"
VALUES = "fragments/drift_values.html"
REFUSED = "fragments/drift_reveal_refused.html"
NOTICE = "fragments/drift_notice.html"
OVERWRITE_FORM = "fragments/drift_overwrite.html"
MAX_NUMBER_DIGITS = 9
MAX_HASH_LENGTH = 128
CONFIRM_MESSAGE = "Confirm the overwrite to continue."


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _plain_number(value: object) -> int | None:
    """The value as a positive whole number, None for anything else."""
    text = value if isinstance(value, str) else ""
    if text.isdecimal() and len(text) <= MAX_NUMBER_DIGITS and int(text) > 0:
        return int(text)
    return None


def _text(form: FormData, name: str) -> str:
    """A text field of the form, empty when missing."""
    value = form.get(name)
    return value[:MAX_HASH_LENGTH] if isinstance(value, str) else ""


def _redirect(location: str) -> Response:
    """Send the page to ``location`` once an action succeeded."""
    return Response(
        status_code=200,
        headers={"HX-Redirect": location, "Cache-Control": "no-store", "Vary": "HX-Request"},
    )


def _overwrite_notice(result: OverwriteResult) -> tuple[str, str]:
    """The tone and sentence of the toast after an overwrite, from what the deploy did."""
    if result.unchanged:
        message = f"The target already matched version {result.version_number}."
    else:
        message = f"Overwrote the target with version {result.version_number}."
        if result.backed_up:
            message += " The previous file was kept as a backup next to it."
    if result.post_deploy is PostDeployStatus.FAILED:
        return "warning", f"{message} The post-deploy command failed, see the service page."
    return "success", message


@router.get("/environments/{environment_id:int}/drift", include_in_schema=False)
async def drift_page(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    imported: str = "",
    overwritten: str = "",
    backup: str = "",
    same: str = "",
    post: str = "",
) -> Response:
    """The masked comparison of the target on disk, the last deployed and the managed version.

    The file is read now. ``imported`` and ``overwritten`` (version numbers) show the toast of
    the action that redirected here; ``backup``, ``same`` and ``post`` describe the overwrite.
    """
    view = await load_drift_view(db, get_crypto(request), access.env)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no version saved")
    result_toast: tuple[str, str] | None = None
    if (number := _plain_number(imported)) is not None:
        result_toast = ("success", f"Imported the target as version {number}. Pending deploy.")
    elif (number := _plain_number(overwritten)) is not None:
        result_toast = _overwrite_notice(
            OverwriteResult(
                version_number=number,
                unchanged=same == "1",
                backed_up=backup == "1",
                post_deploy=PostDeployStatus.FAILED if post == "failed" else PostDeployStatus.NONE,
            )
        )
    return render(
        request,
        PAGE,
        {
            "env": access.env,
            "view": view,
            "result_toast": result_toast,
            "error": None,
            "deploy_timeout_ms": DEPLOY_TIMEOUT_MS,
        },
    )


@router.post("/environments/{environment_id:int}/drift/reveal", include_in_schema=False)
async def reveal_values(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.REVEAL_SECRETS))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The values of the differing keys of the chosen columns, audited before they are returned.

    The form carries what the masked page was built from (versions and the keyed hash of the
    file); when any of it changed, the answer is a notice and holds no value.
    """
    form = await request.form()
    managed = _plain_number(form.get("managed"))
    deployed = _plain_number(form.get("deployed"))
    columns = {name for name in _text(form, "columns").split(",") if name in REVEAL_COLUMNS}
    if managed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    try:
        revealed = await reveal_drift(
            db,
            get_crypto(request),
            access.env,
            actor=access.auth.user,
            ip_address=_client_ip(request),
            columns=columns,
            managed_number=managed,
            deployed_number=deployed,
            target_hash=_text(form, "target"),
        )
    except DriftChangedError as error:
        return render(request, REFUSED, {"error": str(error)}, status_code=409)
    except DriftError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found") from error
    return render(request, VALUES, {"revealed": revealed.values})


@router.post("/environments/{environment_id:int}/import", include_in_schema=False)
async def import_file(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.EDIT_VALUES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Save the target as read now as a new version, then reload the view.

    The form carries the managed version and the keyed hash of the file the page showed. A
    refusal swaps the notice with the reason (422) and saves nothing.
    """
    form = await request.form()
    try:
        version = await import_target(
            db,
            get_crypto(request),
            access.env,
            actor=access.auth.user,
            ip_address=_client_ip(request),
            base_number=_plain_number(form.get("base_version")) or 0,
            target_hash=_text(form, "target_hash"),
        )
    except DriftError as error:
        return render(request, NOTICE, {"error": str(error)}, status_code=422)
    return _redirect(f"/environments/{access.env.id}/drift?imported={version.number}")


def _overwrite_form(
    request: Request, access: EnvironmentContext, base_version: int, error: str
) -> Response:
    """The overwrite dialog form again with the reason, as a 422 swap."""
    return render(
        request,
        OVERWRITE_FORM,
        {
            "env": access.env,
            "managed_number": base_version,
            "error": error,
            "deploy_timeout_ms": DEPLOY_TIMEOUT_MS,
        },
        status_code=422,
    )


@router.post("/environments/{environment_id:int}/overwrite", include_in_schema=False)
async def overwrite(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.DEPLOY))],
    db: Annotated[AsyncSession, Depends(get_db)],
    engine: Annotated[DeployEngine, Depends(get_deploy_engine)],
) -> Response:
    """Replace the target with the managed version as a forced deploy of this one file.

    Nothing is deployed without the confirmation field the dialog posts. A refusal swaps the
    dialog form with the reason (422); success reloads the view with a toast.
    """
    form = await request.form()
    base_version = _plain_number(form.get("base_version")) or 0
    if form.get("confirm_overwrite") != "1":
        return _overwrite_form(request, access, base_version, CONFIRM_MESSAGE)
    try:
        result = await overwrite_target(
            db,
            engine,
            access.scope,
            AuditActor.for_user(access.auth.user, _client_ip(request)),
            access.env,
            base_number=base_version,
        )
    except DriftError as error:
        return _overwrite_form(request, access, base_version, str(error))
    except DeployRequestError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    post = "failed" if result.post_deploy is PostDeployStatus.FAILED else "ok"
    return _redirect(
        f"/environments/{access.env.id}/drift?overwritten={result.version_number}"
        f"&backup={int(result.backed_up)}&same={int(result.unchanged)}&post={post}"
    )
