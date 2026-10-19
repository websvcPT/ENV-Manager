"""Service detail routes: the page, its refreshable panels and the post-deploy settings."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.log import get_logger
from app.core.templating import render
from app.models import ServiceHook
from app.services.access_grid import NodeKind
from app.services.app_settings import load_stored_settings
from app.services.audit import AuditActor
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
    CAPABILITY_TARGETS,
    AccessScope,
    Capability,
    ItemLevel,
    ItemRef,
    Target,
    permitted_environment_ids,
)
from app.services.service_detail import (
    HookSettings,
    ServiceHeader,
    load_environment_rows,
    load_post_deploy,
    load_service_header,
    load_service_history,
)
from app.web.access_tabs import AccessSubject, mount_access_routes
from app.web.dependencies import (
    AuthContext,
    get_crypto,
    get_db,
    get_docker_cli,
    get_scope,
    require_auth,
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


@dataclass(frozen=True)
class ServiceContext:
    """The caller, their scope and the service as they see it."""

    auth: AuthContext
    scope: AccessScope
    service: ServiceHeader


def service_access(
    capability: Capability | None = None,
) -> Callable[..., Awaitable[ServiceContext]]:
    """A dependency that loads a service for the caller and checks an optional capability.

    A service outside the caller's scope answers 404, so its existence is not revealed; a
    capability the caller's group lacks on a service in scope answers 403 (capabilities that
    need an environment are not supported here).
    """

    async def load(
        service_id: int,
        auth: Annotated[AuthContext, Depends(require_auth)],
        scope: Annotated[AccessScope, Depends(get_scope)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> ServiceContext:
        service = await load_service_header(db, scope, service_id)
        if service is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
        if capability is not None:
            item = ItemRef(
                level=ItemLevel.SERVICE,
                project_id=service.project_id,
                deployment_id=service.deployment_id,
                service_id=service.id,
            )
            target = item if CAPABILITY_TARGETS[capability] is Target.ITEM else None
            if not scope.can(capability, target):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return ServiceContext(auth=auth, scope=scope, service=service)

    return load


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
    db: AsyncSession,
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

    ``review`` holds the parsed commands to show before they are stored.
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
        "post_deploy_enabled": (await load_stored_settings(db)).post_deploy.enabled,
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
    added: str = "",
) -> Response:
    """The service page: header, explanation, environments, post-deploy panel and history.

    ``added`` (service or environment) shows the toast after the add forms redirect here.
    """
    rows = await load_environment_rows(db, access.scope, access.service)
    history = await load_service_history(db, access.service)
    post_deploy = await load_post_deploy(db, access.service, history)
    deployable = await permitted_environment_ids(
        db, access.scope, Capability.DEPLOY, service_id=access.service.id
    )
    return render(
        request,
        "pages/service.html",
        {
            **_flags(access),
            "can_deploy": bool(deployable) and not access.service.outside_allowed,
            **await _settings_context(db, access, cli, post_deploy.hook),
            "environments": rows,
            "pending": sum(1 for row in rows if row.status == "pending"),
            "history": history,
            "post_deploy": post_deploy,
            "added": added if added in ADDED_TOASTS else "",
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
) -> Response:
    """The post-deploy panel, refreshed when a deploy finishes."""
    history = await load_service_history(db, access.service)
    post_deploy = await load_post_deploy(db, access.service, history)
    return render(
        request,
        POST_DEPLOY_FRAGMENT,
        {
            **_flags(access),
            **await _settings_context(db, access, cli, post_deploy.hook),
            "post_deploy": post_deploy,
        },
    )


@router.post("/services/{service_id:int}/postdeploy", include_in_schema=False)
async def save_post_deploy(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
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
    actor = AuditActor.for_user(access.auth.user, client_address(request))
    try:
        hook = await save_post_deploy_hook(db, cli, access.scope, actor, access.service.id, typed)
    except HookForbiddenError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted") from error
    except HookReviewRequired as review:
        context = await _settings_context(db, access, cli, None, form=typed, review=review)
        return render(request, SETTINGS_FRAGMENT, {**_flags(access), **context})
    except HookSettingsError as error:
        context = await _settings_context(db, access, cli, None, form=typed, errors=error.errors)
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
    context = await _settings_context(db, access, cli, hook, saved=saved)
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
    actor = AuditActor.for_user(access.auth.user, client_address(request))
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
