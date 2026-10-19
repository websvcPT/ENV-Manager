"""The Settings screens that share this module: the home redirect, security, post-deploy, general.

Superadmin only: every route here holds the settings capability, so any other group gets 403 and
the sidebar shows no link. Saves are validated on the server, audited with the old and new values
and effective at once, because every reader loads the stored settings per request.
"""

import os
from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.client_address import client_address
from app.core.display_time import DATE_FORMATS, zone_names
from app.core.templating import render
from app.services.app_settings import StoredSettings
from app.services.audit import AuditActor
from app.services.diagnostics import collect_diagnostics
from app.services.docker_cli import DockerCli, DockerStatus
from app.services.general_settings import (
    DIST_MISSING_CHOICES,
    DRIFT_CHOICES,
    MISSING_TARGET_CHOICES,
    GeneralForm,
    GeneralFormError,
    save_general,
    validate_general,
)
from app.services.post_deploy_settings import PostDeploySwitchError, save_post_deploy_switch
from app.services.security_settings import (
    SecurityForm,
    SecurityFormError,
    save_security,
    validate_security,
)
from app.web.dependencies import get_db, get_docker_cli, get_stored_settings
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

SECURITY_FRAGMENT = "fragments/settings_security.html"
POST_DEPLOY_FRAGMENT = "fragments/settings_post_deploy.html"
GENERAL_FRAGMENT = "fragments/settings_general.html"
TIME_ZONE_SETTING = "general.time_zone"

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Docker = Annotated[DockerCli, Depends(get_docker_cli)]


def _saved(changed: bool, title: str, message: str) -> dict[str, str]:
    """The toast of a save: success when something was stored, a notice when nothing changed."""
    if changed:
        return {"tone": "success", "title": title, "message": message}
    return {"tone": "info", "title": "Nothing to save", "message": "The settings are unchanged."}


def _actor(request: Request, context: ManageSettings) -> AuditActor:
    """The signed-in superadmin as the actor of an audit entry."""
    return AuditActor.for_user(context.user, client_address(request))


@router.get("/settings", include_in_schema=False)
async def settings_home(_context: ManageSettings) -> Response:
    """The Settings address the sidebar links to opens the first tab."""
    return RedirectResponse("/settings/paths", status_code=status.HTTP_303_SEE_OTHER)


def _security_context(
    request: Request,
    form: SecurityForm,
    *,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the security form needs: values, refusals by field, the toast and the fixed limit."""
    return {
        "form": form,
        "errors": errors or {},
        "saved": saved,
        "address_limit_fixed": request.app.state.login_ip_max_failures is not None,
    }


def _effective_security(request: Request, stored: StoredSettings) -> SecurityForm:
    """The form of the stored settings, with the limit the startup override fixes (if any)."""
    form = SecurityForm.from_settings(stored.security)
    override = request.app.state.login_ip_max_failures
    return form if override is None else _with_address_limit(form, override)


def _with_address_limit(form: SecurityForm, limit: int) -> SecurityForm:
    """The form with the sign-in limit per address replaced, for display."""
    return replace(form, ip_max_failures=str(limit))


@router.get("/settings/security", include_in_schema=False)
async def security_page(request: Request, _context: ManageSettings, stored: Stored) -> Response:
    """The Security tab: session timeouts, lockout limits, password length and two-factor policy."""
    form = _effective_security(request, stored)
    page = {**tab_context("security"), **_security_context(request, form)}
    return render(request, "pages/settings_security.html", page)


@router.post("/settings/security", include_in_schema=False)
async def save_security_route(
    request: Request,
    db: Database,
    context: ManageSettings,
    stored: Stored,
    idle_timeout_minutes: Annotated[str, Form()] = "",
    absolute_timeout_hours: Annotated[str, Form()] = "",
    user_max_failures: Annotated[str, Form()] = "",
    ip_max_failures: Annotated[str, Form()] = "",
    api_ip_max_failures: Annotated[str, Form()] = "",
    lockout_minutes: Annotated[str, Form()] = "",
    min_password_length: Annotated[str, Form()] = "",
) -> Response:
    """Save the security settings. A refused value answers the form again with the reasons (422).

    The sign-in limit per address is kept as stored while the startup override fixes it.
    """
    form = SecurityForm(
        idle_timeout_minutes=idle_timeout_minutes,
        absolute_timeout_hours=absolute_timeout_hours,
        user_max_failures=user_max_failures,
        ip_max_failures=ip_max_failures,
        api_ip_max_failures=api_ip_max_failures,
        lockout_minutes=lockout_minutes,
        min_password_length=min_password_length,
    )
    fixed = request.app.state.login_ip_max_failures is not None
    try:
        clean = validate_security(form, stored=stored.security, address_limit_fixed=fixed)
    except SecurityFormError as error:
        override = request.app.state.login_ip_max_failures
        shown = _with_address_limit(form, override) if fixed else form
        return render(
            request,
            SECURITY_FRAGMENT,
            _security_context(request, shown, errors=error.errors),
            status_code=422,
        )
    changes = await save_security(db, clean, actor=_actor(request, context))
    saved = _saved(bool(changes), "Security settings saved", "They apply now.")
    shown = _effective_security(request, StoredSettings(security=clean))
    return render(request, SECURITY_FRAGMENT, _security_context(request, shown, saved=saved))


def _post_deploy_context(
    stored: StoredSettings,
    cli: DockerCli,
    docker: DockerStatus,
    *,
    error: str | None = None,
    saved: dict[str, str] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """What the post-deploy tab needs: the Docker status, the switch as shown and the messages."""
    switch = stored.post_deploy.enabled if enabled is None else enabled
    return {
        "docker": docker,
        "socket_path": str(cli.socket_path),
        "enabled": switch and docker.available,
        "error": error,
        "saved": saved,
    }


@router.get("/settings/post-deploy", include_in_schema=False)
async def post_deploy_page(
    request: Request, _context: ManageSettings, stored: Stored, cli: Docker
) -> Response:
    """The Post-deploy tab: Docker socket status and the switch for post-deploy commands."""
    page = {**tab_context("post-deploy"), **_post_deploy_context(stored, cli, await cli.status())}
    return render(request, "pages/settings_post_deploy.html", page)


@router.post("/settings/post-deploy", include_in_schema=False)
async def save_post_deploy_route(
    request: Request,
    db: Database,
    context: ManageSettings,
    stored: Stored,
    cli: Docker,
    enabled: Annotated[str, Form()] = "",
) -> Response:
    """Save the switch. It is refused while Docker is unusable, with the reason in the form."""
    wanted = enabled.lower() in {"on", "true", "1", "yes"}
    docker = await cli.status()
    try:
        changes = await save_post_deploy_switch(
            db, wanted, docker=docker, actor=_actor(request, context)
        )
    except PostDeploySwitchError as error:
        page = _post_deploy_context(stored, cli, docker, error=str(error))
        return render(request, POST_DEPLOY_FRAGMENT, page, status_code=409)
    message = (
        "Services with a command run it after a deploy."
        if wanted
        else "Deploys skip the stored commands and say why."
    )
    saved = _saved(bool(changes), "Post-deploy settings saved", message)
    page = _post_deploy_context(stored, cli, docker, saved=saved, enabled=wanted)
    return render(request, POST_DEPLOY_FRAGMENT, page)


def _general_context(
    form: GeneralForm,
    *,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the general form needs: values, refusals by field, the choices and the toast."""
    return {
        "form": form,
        "errors": errors or {},
        "saved": saved,
        "zones": [(name, name) for name in zone_names()],
        "date_formats": [(key, label) for key, (label, _day, _clock) in DATE_FORMATS.items()],
        "dist_choices": DIST_MISSING_CHOICES,
        "drift_choices": DRIFT_CHOICES,
        "target_choices": MISSING_TARGET_CHOICES,
    }


@router.get("/settings/general", include_in_schema=False)
async def general_page(request: Request, _context: ManageSettings, stored: Stored) -> Response:
    """The General tab: display settings, deployment defaults and the read-only diagnostics."""
    diagnostics = collect_diagnostics(get_settings(), os.environ, client_address(request))
    page = {
        **tab_context("general"),
        **_general_context(GeneralForm.from_settings(stored.general)),
        "diagnostics": diagnostics,
    }
    return render(request, "pages/settings_general.html", page)


@router.post("/settings/general", include_in_schema=False)
async def save_general_route(
    request: Request,
    db: Database,
    context: ManageSettings,
    time_zone: Annotated[str, Form()] = "",
    date_format: Annotated[str, Form()] = "",
    dist_missing_policy: Annotated[str, Form()] = "",
    drift_policy: Annotated[str, Form()] = "",
    missing_target_policy: Annotated[str, Form()] = "",
    file_mode: Annotated[str, Form()] = "",
    file_owner: Annotated[str, Form()] = "",
    file_group: Annotated[str, Form()] = "",
) -> Response:
    """Save the general settings. A refused value answers the form again with the reasons (422)."""
    form = GeneralForm(
        time_zone=time_zone,
        date_format=date_format,
        dist_missing_policy=dist_missing_policy,
        drift_policy=drift_policy,
        missing_target_policy=missing_target_policy,
        file_mode=file_mode,
        file_owner=file_owner,
        file_group=file_group,
    )
    try:
        clean = validate_general(form)
    except GeneralFormError as error:
        return render(
            request,
            GENERAL_FRAGMENT,
            _general_context(form, errors=error.errors),
            status_code=422,
        )
    changes = await save_general(db, clean, actor=_actor(request, context))
    if any(change.setting == TIME_ZONE_SETTING for change in changes):
        await request.app.state.backup_scheduler.reload()
    saved = _saved(bool(changes), "General settings saved", "They apply now.")
    return render(
        request,
        GENERAL_FRAGMENT,
        _general_context(GeneralForm.from_settings(clean), saved=saved),
    )
