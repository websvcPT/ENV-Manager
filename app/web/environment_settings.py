"""Environment settings routes: edit with the policy overrides and the live target path check.
Superadmin only.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Deployment, Environment
from app.services.environment_settings import (
    EnvironmentSettingsForm,
    deployed_state,
    update_settings,
    validate_settings,
)
from app.services.environments import EnvironmentFormError, relative_path, target_hint
from app.services.paths import load_allowed_paths
from app.services.permissions import Capability
from app.web.dependencies import get_db
from app.web.editor import EnvironmentContext, environment_access
from app.web.htmx import hx_redirect

router = APIRouter()

PAGE = "pages/environment_settings.html"
FORM_FRAGMENT = "fragments/environment_settings_form.html"
CHECK_FRAGMENT = "fragments/environment_target_check.html"

Structure = Annotated[EnvironmentContext, Depends(environment_access(Capability.MANAGE_STRUCTURE))]


async def _stored(db: AsyncSession, access: EnvironmentContext) -> tuple[Environment, Deployment]:
    """The stored environment and deployment the authorised context was loaded from."""
    environment = await db.get(Environment, access.env.id)
    deployment = await db.get(Deployment, access.env.deployment_id)
    if environment is None or deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "environment not found")
    return environment, deployment


async def _check_context(
    db: AsyncSession,
    environment: Environment,
    deployment: Deployment,
    target_path: str,
    acknowledged: bool,
    error: str | None = None,
) -> dict[str, Any]:
    """The line under the target path field and the notice for a changed target path."""
    allowed = await load_allowed_paths(db)
    hint = target_hint(allowed, deployment.root_path, target_path)
    normalized, _, problem = relative_path(allowed, deployment.root_path, target_path, "Target")
    moved = problem is None and normalized is not None and normalized != environment.target_path
    return {
        "hint": hint,
        "service_id": environment.service_id,
        "environment": environment,
        "moved": moved,
        "new_path": normalized,
        "deployed": await deployed_state(db, environment) if moved else None,
        "acknowledged": acknowledged,
        "error": None,
        "ack_error": error,
    }


async def _form_context(
    db: AsyncSession,
    access: EnvironmentContext,
    environment: Environment,
    deployment: Deployment,
    form: EnvironmentSettingsForm,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the form needs: values, errors, the deployment's policies and the target check."""
    errors = errors or {}
    check = await _check_context(
        db,
        environment,
        deployment,
        form.target_path,
        form.acknowledge_move,
        errors.get("acknowledge_move"),
    )
    check["error"] = errors.get("target_path")
    return {
        "env": access.env,
        "form": form,
        "errors": errors,
        "deployment": deployment,
        "check": check,
        "action": f"/environments/{environment.id}/settings",
        "cancel_href": f"/environments/{environment.id}",
    }


@router.get("/environments/{environment_id:int}/settings", include_in_schema=False)
async def settings_page(
    request: Request, access: Structure, db: Annotated[AsyncSession, Depends(get_db)]
) -> Response:
    """The settings form with the stored values."""
    environment, deployment = await _stored(db, access)
    form = EnvironmentSettingsForm.from_environment(environment)
    return render(request, PAGE, await _form_context(db, access, environment, deployment, form))


@router.get("/environments/{environment_id:int}/settings/target-check", include_in_schema=False)
async def settings_target_check(
    request: Request,
    access: Structure,
    db: Annotated[AsyncSession, Depends(get_db)],
    target_path: str = "",
) -> Response:
    """The line under the target path field, with the notice when the path would change."""
    environment, deployment = await _stored(db, access)
    context = await _check_context(db, environment, deployment, target_path, False)
    return render(request, CHECK_FRAGMENT, {"check": context})


@router.post("/environments/{environment_id:int}/settings", include_in_schema=False)
async def save_settings(
    request: Request,
    access: Structure,
    db: Annotated[AsyncSession, Depends(get_db)],
    target_path: Annotated[str, Form()] = "",
    file_type: Annotated[str, Form()] = "",
    dist_path: Annotated[str, Form()] = "",
    file_mode: Annotated[str, Form()] = "",
    file_owner: Annotated[str, Form()] = "",
    file_group: Annotated[str, Form()] = "",
    dist_missing_policy: Annotated[str, Form()] = "",
    drift_policy: Annotated[str, Form()] = "",
    missing_target_policy: Annotated[str, Form()] = "",
    acknowledge_move: Annotated[str, Form()] = "",
) -> Response:
    """Save the settings. A refused value swaps the form again with the reasons (422)."""
    environment, deployment = await _stored(db, access)
    form = EnvironmentSettingsForm(
        target_path=target_path,
        file_type=file_type,
        dist_path=dist_path,
        file_mode=file_mode,
        file_owner=file_owner,
        file_group=file_group,
        dist_missing_policy=dist_missing_policy,
        drift_policy=drift_policy,
        missing_target_policy=missing_target_policy,
        acknowledge_move=bool(acknowledge_move),
    )
    try:
        clean = await validate_settings(
            db, await load_allowed_paths(db), deployment, environment, form
        )
    except EnvironmentFormError as error:
        return render(
            request,
            FORM_FRAGMENT,
            await _form_context(db, access, environment, deployment, form, error.errors),
            status_code=422,
        )
    await update_settings(
        db,
        environment,
        access.env,
        clean,
        actor=access.auth.user,
        ip_address=client_address(request),
    )
    return hx_redirect(f"/environments/{environment.id}?updated=true")
