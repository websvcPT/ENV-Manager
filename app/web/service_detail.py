"""Service detail routes: the page, its refreshable panels and the post-deploy settings."""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.log import get_logger
from app.core.templating import render
from app.models import Deployment, ServiceHook
from app.services.access_grid import NodeKind
from app.services.app_settings import StoredSettings
from app.services.docker_cli import DockerCli
from app.services.hook_settings import (
    HookForbiddenError,
    HookForm,
    HookReviewRequired,
    HookSettingsError,
    check_compose_setup,
    save_post_deploy_hook,
)
from app.services.permissions import (
    CAPABILITY_GROUPS,
    AccessScope,
    Capability,
    ItemLevel,
    ItemRef,
)
from app.services.service_detail import (
    HookSettings,
    ServiceHeader,
    load_environment_rows,
    load_post_deploy,
    load_service_header,
    load_service_history,
)
from app.services.services import is_deployment_files
from app.web.access_tabs import AccessSubject, mount_access_routes
from app.web.dependencies import (
    AuthContext,
    audit_actor,
    get_crypto,
    get_db,
    get_docker_cli,
    get_stored_settings,
    item_access,
)

log = get_logger(__name__)
router = APIRouter()

ENVIRONMENTS_FRAGMENT = "fragments/service_environments.html"
POST_DEPLOY_FRAGMENT = "fragments/service_postdeploy.html"
SETTINGS_FRAGMENT = "fragments/service_postdeploy_settings.html"
TEST_RESULT_FRAGMENT = "fragments/service_test_result.html"
HISTORY_FRAGMENT = "fragments/service_history.html"
ADDED_TOASTS = ("service", "environment")
SAVED_MESSAGE = "They apply from the next deploy."
SAVED_EMPTY_MESSAGE = "No command is set, so deploys run none."
NOTHING_STORED_MESSAGE = "No command is set, so nothing was stored."

Stored = Annotated[StoredSettings, Depends(get_stored_settings)]


@dataclass(frozen=True)
class ServiceContext:
    """The caller, their scope and the service as they see it."""

    auth: AuthContext
    scope: AccessScope
    service: ServiceHeader


# Capabilities that need an environment are not supported on a service.
service_access = item_access(
    "service_id",
    load_service_header,
    lambda service: ItemRef(
        level=ItemLevel.SERVICE,
        project_id=service.project_id,
        deployment_id=service.deployment_id,
        service_id=service.id,
    ),
    "service not found",
    ServiceContext,
)


async def _compose_context(db: AsyncSession, access: ServiceContext) -> dict[str, Any]:
    """The read only Compose files panel of a deployment files page: the Compose files linked to
    their deployment, for a caller who reaches the whole deployment; nothing for a service."""
    if not is_deployment_files(access.service.name) or not access.service.deployment_full:
        return {}
    deployment = await db.get(Deployment, access.service.deployment_id)
    return {
        "compose_deployment_id": access.service.deployment_id,
        "compose_files": tuple(deployment.compose_files or ()) if deployment else (),
        "can_view_compose": access.scope.active
        and access.scope.group in CAPABILITY_GROUPS[Capability.VIEW_COMPOSE_FILES],
        "can_link_compose": False,
    }


def _flags(access: ServiceContext) -> dict[str, Any]:
    """What every panel template needs besides its rows: the service and what may be edited."""
    return {
        "service": access.service,
        "can_manage_structure": access.scope.can(Capability.MANAGE_STRUCTURE),
        "can_manage_grants": access.scope.can(Capability.MANAGE_GRANTS),
        "is_viewer": access.scope.group not in CAPABILITY_GROUPS[Capability.EDIT_VALUES],
    }


async def _access_subject(
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_GRANTS))],
) -> AccessSubject:
    """The service of an Access tab request, for the superadmin."""
    return AccessSubject(
        NodeKind.SERVICE,
        access.service.id,
        access.service.project_id,
        access.auth,
        "pages/service_access.html",
        _flags(access),
    )


mount_access_routes(router, NodeKind.SERVICE, _access_subject)


def _form_of(hook: HookSettings | ServiceHook | None) -> HookForm:
    """The settings form as it reads for a stored command, the defaults when there is none."""
    if hook is None:
        return HookForm()
    return HookForm(
        working_directory=hook.working_directory or "",
        commands=hook.commands,
        timeout=str(hook.timeout_seconds),
        run_only_if_changed=hook.run_only_if_changed,
    )


async def _settings_context(
    stored: StoredSettings,
    access: ServiceContext,
    cli: DockerCli,
    hook: HookSettings | ServiceHook | None,
    *,
    form: HookForm | None = None,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
    review: HookReviewRequired | None = None,
) -> dict[str, Any]:
    """What the settings form needs; only the superadmin gets the Docker status and the form.

    ``stored`` is the settings of the request, ``review`` holds the parsed commands to show
    before they are stored.
    """
    if not access.service.can_view_command:
        return {
            "docker": None,
            "socket_path": "",
            "form": HookForm(),
            "errors": {},
            "saved": None,
            "review": None,
        }
    return {
        "docker": await cli.status(),
        "post_deploy_enabled": stored.post_deploy.enabled,
        "socket_path": str(cli.socket_path),
        "form": form or _form_of(hook),
        "errors": errors or {},
        "saved": saved,
        "review": review,
    }


@router.get("/services/{service_id:int}", include_in_schema=False)
async def service_page(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    stored: Stored,
    added: str = "",
    renamed: bool = False,
    deleted: str = "",
) -> Response:
    """The service page: header, explanation, environments, post-deploy panel and history.

    ``added`` (service or environment) shows the toast after the add forms redirect here,
    ``renamed`` the one after the rename form and ``deleted`` (environment) the one after a
    file was deleted.
    """
    rows = await load_environment_rows(db, access.scope, access.service)
    history = await load_service_history(db, access.service)
    post_deploy = await load_post_deploy(db, access.service, history)
    deployable = any(row.can_check_dist for row in rows)
    return render(
        request,
        "pages/service.html",
        {
            **_flags(access),
            "can_deploy": deployable and not access.service.outside_allowed,
            **await _settings_context(stored, access, cli, post_deploy.hook),
            "environments": rows,
            "pending": sum(1 for row in rows if row.status == "pending"),
            "history": history,
            "post_deploy": post_deploy,
            "added": added if added in ADDED_TOASTS else "",
            "renamed": renamed,
            "deleted": "environment" if deleted == "environment" else "",
            **await _compose_context(db, access),
        },
    )


@router.get("/services/{service_id:int}/environments", include_in_schema=False)
async def environments_table(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The environments panel. A failed load answers an error panel with Retry."""
    try:
        rows = await load_environment_rows(db, access.scope, access.service)
    except SQLAlchemyError:
        log.exception("service environments could not be loaded")
        return render(
            request,
            ENVIRONMENTS_FRAGMENT,
            {**_flags(access), "environments": (), "load_failed": True},
            status_code=503,
        )
    return render(request, ENVIRONMENTS_FRAGMENT, {**_flags(access), "environments": rows})


@router.get("/services/{service_id:int}/postdeploy", include_in_schema=False)
async def post_deploy_panel(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    stored: Stored,
) -> Response:
    """The post-deploy panel, refreshed when a deploy finishes."""
    history = await load_service_history(db, access.service)
    post_deploy = await load_post_deploy(db, access.service, history)
    return render(
        request,
        POST_DEPLOY_FRAGMENT,
        {
            **_flags(access),
            **await _settings_context(stored, access, cli, post_deploy.hook),
            "post_deploy": post_deploy,
        },
    )


@router.post("/services/{service_id:int}/postdeploy", include_in_schema=False)
async def save_post_deploy(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    stored: Stored,
    working_directory: Annotated[str, Form()] = "",
    commands: Annotated[str, Form()] = "",
    timeout: Annotated[str, Form()] = "",
    run_only_if_changed: Annotated[str | None, Form()] = None,
    reviewed: Annotated[str, Form()] = "",
) -> Response:
    """Save the post-deploy settings (superadmin).

    A refusal swaps the form with its reasons. Changed commands are not stored by the first
    save: the form comes back (200) with the parsed arguments of every line and a confirming
    save, and only that second save stores them.
    """
    typed = HookForm(
        working_directory, commands, timeout, run_only_if_changed is not None, reviewed
    )
    actor = audit_actor(request, access.auth)
    try:
        hook = await save_post_deploy_hook(db, cli, access.scope, actor, access.service.id, typed)
    except HookForbiddenError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted") from error
    except HookReviewRequired as review:
        context = await _settings_context(stored, access, cli, None, form=typed, review=review)
        return render(request, SETTINGS_FRAGMENT, {**_flags(access), **context})
    except HookSettingsError as error:
        context = await _settings_context(
            stored, access, cli, None, form=typed, errors=error.errors
        )
        return render(
            request,
            SETTINGS_FRAGMENT,
            {**_flags(access), **context},
            status_code=422,
        )
    if hook is None:
        saved = {"tone": "info", "title": "Nothing to save", "message": NOTHING_STORED_MESSAGE}
    else:
        message = SAVED_MESSAGE if hook.commands else SAVED_EMPTY_MESSAGE
        saved = {"tone": "success", "title": "Post-deploy settings saved", "message": message}
    context = await _settings_context(stored, access, cli, hook, saved=saved)
    return render(request, SETTINGS_FRAGMENT, {**_flags(access), **context})


@router.post("/services/{service_id:int}/postdeploy/test", include_in_schema=False)
async def test_post_deploy(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    working_directory: Annotated[str, Form()] = "",
) -> Response:
    """Run the Compose check in the typed working directory (superadmin); the answer is a line."""
    actor = audit_actor(request, access.auth)
    try:
        check = await check_compose_setup(
            db, crypto, cli, access.scope, actor, access.service.id, working_directory
        )
    except HookForbiddenError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted") from error
    except HookSettingsError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found") from error
    return render(request, TEST_RESULT_FRAGMENT, {"check": check})


@router.get("/services/{service_id:int}/history", include_in_schema=False)
async def service_history(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The deploy history panel, refreshed when a deploy finishes."""
    history = await load_service_history(db, access.service)
    return render(request, HISTORY_FRAGMENT, {**_flags(access), "history": history})
