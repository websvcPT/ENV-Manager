"""The post-deploy command settings of a service: saving them and checking the Compose setup.

Only the superadmin changes commands. A save is refused while the Docker socket, the daemon or
the compose plugin is not usable, and every line is validated (``app.services.hook_commands``,
safety net included) and the working directory resolved against the allowed paths before anything
is stored; the runner validates again when it executes. Changed commands are stored only after
the parsed arguments of every line were shown: the first save answers ``HookReviewRequired`` with
them and a digest of the reviewed text, and the save that sends the digest back stores exactly
that text. Audit entries name the settings that changed and never a path. A change of the
commands also records the old and the new command text, which the audit log shows to the
superadmin only (``app.services.audit_details.COMMAND_TEXT_KEYS``), like the command itself. The
compose check runs ``docker compose config --quiet`` in the working directory.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AuditResult, Deployment, HookType, Service, ServiceHook
from app.services.audit import AuditActor, record_actor_audit
from app.services.docker_cli import DockerCli
from app.services.hook_commands import Command, CommandError, normalize_commands, parse_commands
from app.services.hook_process import BoundedOutput, run_process
from app.services.hook_runner import WorkingDirectoryError, resolve_working_directory
from app.services.paths import PathValidationError, load_allowed_paths
from app.services.permissions import AccessScope, Capability
from app.services.scrub_values import load_scrubber
from app.services.structure_lookup import service_with_deployment

ACTION_SAVE = "service.hook_save"
ACTION_CHECK = "service.hook_check"
DEFAULT_TIMEOUT_SECONDS = 120
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600
CHECK_TIMEOUT_SECONDS = 30

SERVICE_NOT_FOUND = "Service not found."
TIMEOUT_MESSAGE = (
    f"Enter a whole number of seconds from {MIN_TIMEOUT_SECONDS} to {MAX_TIMEOUT_SECONDS}."
)
UNAVAILABLE_SUFFIX = "Post-deploy commands are off, so settings cannot be saved."
CHECK_UNAVAILABLE_SUFFIX = "Post-deploy commands are off, so the check cannot run."
CHECK_OK_MESSAGE = "The working directory and the Compose file are valid."
CHECK_FAILED_MESSAGE = "Docker Compose reported a problem with the working directory."
CHECK_TIMED_OUT_MESSAGE = "The check took too long and was stopped."
CHECK_NOT_STARTED_MESSAGE = "The check could not be started."
FORM_KEY = "form"


class HookForbiddenError(Exception):
    """The caller is not the superadmin."""


class HookSettingsError(Exception):
    """The settings were refused. ``errors`` maps a field name (or ``form``) to its message."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the safe messages by field."""
        super().__init__("; ".join(errors.values()))
        self.errors = errors


class HookReviewRequired(Exception):
    """Changed commands must be looked at before they are stored.

    ``commands`` holds the parsed arguments of each command and ``digest`` identifies the text
    that was parsed; sending it back with the same commands confirms them.
    """

    def __init__(self, commands: tuple[Command, ...], digest: str) -> None:
        """Hold what the review shows."""
        super().__init__("the commands were not reviewed")
        self.commands = commands
        self.digest = digest


@dataclass(frozen=True)
class HookForm:
    """The settings as typed: text for the three inputs and the checkbox.

    ``reviewed`` is the digest of the commands the person was shown the parsed form of.
    """

    working_directory: str = ""
    commands: str = ""
    timeout: str = str(DEFAULT_TIMEOUT_SECONDS)
    run_only_if_changed: bool = True
    reviewed: str = ""


@dataclass(frozen=True)
class ComposeCheck:
    """The answer of the compose check: ``output`` is the truncated output of a failed check."""

    ok: bool
    message: str
    output: str | None = None


def _require_superadmin(scope: AccessScope) -> None:
    """Refuse everyone but the superadmin."""
    if not scope.can(Capability.MANAGE_STRUCTURE):
        raise HookForbiddenError


async def _require_service(db: AsyncSession, service_id: int) -> tuple[Service, Deployment]:
    """The service and its deployment, or a refusal that the service does not exist."""
    chain = await service_with_deployment(db, service_id)
    if chain is None:
        raise HookSettingsError({FORM_KEY: SERVICE_NOT_FOUND})
    return chain.service, chain.deployment


def commands_digest(commands: str) -> str:
    """The digest that identifies a normalized command text in the review."""
    return hashlib.sha256(commands.encode()).hexdigest()


def _parse_timeout(raw: str) -> int | None:
    """The timeout in seconds, None when it is not a whole number in range."""
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    return value if MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS else None


async def save_post_deploy_hook(
    db: AsyncSession,
    cli: DockerCli,
    scope: AccessScope,
    actor: AuditActor,
    service_id: int,
    form: HookForm,
) -> ServiceHook | None:
    """Validate and store the post-deploy settings of a service, auditing what changed.

    Returns the stored hook, None when there is nothing to store (no command and no hook yet).
    Raises ``HookForbiddenError`` for anyone but the superadmin, ``HookSettingsError`` when the
    Docker socket is not usable or a field is refused, and ``HookReviewRequired`` when the
    commands differ from the stored ones and their digest was not confirmed; nothing is written
    then.
    """
    _require_superadmin(scope)
    service, deployment = await _require_service(db, service_id)
    status = await cli.status()
    if not status.available:
        raise HookSettingsError({FORM_KEY: f"{status.reason} {UNAVAILABLE_SUFFIX}"})

    errors: dict[str, str] = {}
    command_count = 0
    try:
        command_count = len(parse_commands(form.commands))
    except CommandError as error:
        errors["commands"] = str(error)
    working_directory: str | None = None
    if form.working_directory.strip():
        allowed = await load_allowed_paths(db)
        try:
            working_directory = str(
                resolve_working_directory(
                    allowed, Path(deployment.root_path), form.working_directory
                )
            )
        except WorkingDirectoryError as error:
            errors["working_directory"] = str(error)
    timeout = _parse_timeout(form.timeout)
    if timeout is None:
        errors["timeout"] = TIMEOUT_MESSAGE
    if errors or timeout is None:
        raise HookSettingsError(errors)

    commands = normalize_commands(form.commands)
    stored = await db.scalar(
        select(ServiceHook).where(
            ServiceHook.service_id == service.id, ServiceHook.type == HookType.POST_DEPLOY
        )
    )
    previous_commands = stored.commands if stored is not None else ""
    if commands and commands != previous_commands:
        digest = commands_digest(commands)
        if form.reviewed != digest:
            raise HookReviewRequired(parse_commands(commands), digest)
    if stored is None:
        if not commands:
            return None
        stored = ServiceHook(service_id=service.id, type=HookType.POST_DEPLOY)
        db.add(stored)
    changed = [
        name
        for name, before, after in (
            ("commands", stored.commands, commands),
            ("working_directory", stored.working_directory, working_directory),
            ("timeout", stored.timeout_seconds, timeout),
            ("run_only_if_changed", stored.run_only_if_changed, form.run_only_if_changed),
        )
        if before != after
    ]
    stored.commands = commands
    stored.working_directory = working_directory
    stored.timeout_seconds = timeout
    stored.run_only_if_changed = form.run_only_if_changed
    details: dict[str, object] = {
        "hook": HookType.POST_DEPLOY.value,
        "changed": changed,
        "commands": command_count,
    }
    if "commands" in changed:
        details["previous_commands"] = previous_commands
        details["new_commands"] = commands
    if changed:
        await record_actor_audit(
            db,
            actor,
            action=ACTION_SAVE,
            result=AuditResult.SUCCESS,
            target_type="service",
            target_id=service.id,
            target_name=service.name,
            project_id=deployment.project_id,
            deployment_id=deployment.id,
            service_id=service.id,
            details=details,
        )
    await db.commit()
    return stored


async def check_compose_setup(
    db: AsyncSession,
    crypto: CryptoService,
    cli: DockerCli,
    scope: AccessScope,
    actor: AuditActor,
    service_id: int,
    working_directory: str,
) -> ComposeCheck:
    """Run ``docker compose config --quiet`` in the working directory and report the outcome.

    A blank working directory means the deployment root. The directory is resolved against the
    allowed paths now. The check is audited (outcome only). The output of a failed check is
    scrubbed of the configuration values of the deployment, like post-deploy output. Raises
    ``HookForbiddenError`` for anyone but the superadmin and ``HookSettingsError`` when the
    service does not exist.
    """
    _require_superadmin(scope)
    service, deployment = await _require_service(db, service_id)
    result = await _run_compose_check(db, crypto, cli, deployment, working_directory)
    await record_actor_audit(
        db,
        actor,
        action=ACTION_CHECK,
        result=AuditResult.SUCCESS if result.ok else AuditResult.FAILURE,
        target_type="service",
        target_id=service.id,
        target_name=service.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
        details={"hook": HookType.POST_DEPLOY.value, "ok": result.ok},
    )
    await db.commit()
    return result


async def _run_compose_check(
    db: AsyncSession,
    crypto: CryptoService,
    cli: DockerCli,
    deployment: Deployment,
    working_directory: str,
) -> ComposeCheck:
    """The compose check itself, without permission checks or audit."""
    status = await cli.status()
    executable = cli.executable
    if not status.available or executable is None:
        return ComposeCheck(False, f"{status.reason} {CHECK_UNAVAILABLE_SUFFIX}")
    allowed = await load_allowed_paths(db)
    try:
        root = allowed.resolve(deployment.root_path)
        directory = resolve_working_directory(allowed, root, working_directory)
    except PathValidationError:
        return ComposeCheck(False, "The deployment is outside the allowed paths.")
    except WorkingDirectoryError as error:
        return ComposeCheck(False, str(error))
    output = BoundedOutput()
    try:
        outcome = await run_process(
            [executable, "compose", "config", "--quiet"],
            cwd=directory,
            env=cli.environment(),
            timeout=CHECK_TIMEOUT_SECONDS,
            output=output,
        )
    except OSError:
        return ComposeCheck(False, CHECK_NOT_STARTED_MESSAGE)
    if outcome.timed_out:
        return ComposeCheck(False, CHECK_TIMED_OUT_MESSAGE)
    if outcome.exit_code != 0:
        scrubber = await load_scrubber(db, crypto, deployment.id)
        return ComposeCheck(False, CHECK_FAILED_MESSAGE, output.text(scrubber) or None)
    return ComposeCheck(True, CHECK_OK_MESSAGE)
