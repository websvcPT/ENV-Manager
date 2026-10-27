"""Starting, stopping, restarting and recreating one service of a deployment's Compose project.

``prepare_container_action`` decides whether an action can run and builds its command, and
``run_container_action`` runs it: the checks are made again, the deploy lock of the deployment
root is taken (the same lock a deploy holds, so a recreate never overlaps a deploy writing the
files it reads), the docker command runs without a shell and the attempt is audited.

- The service must be a service of the Compose project that has a container, as Docker reports it
  now, and its name must be a plain Compose service name before it is used as an argument.
- With Compose files linked to the deployment every action runs on them (``--project-directory``,
  ``-p`` and one ``-f`` per file), so Compose sees the same model as the user's own commands.
  Without linked files start, stop and restart run on the project alone (``-p``), which Compose
  resolves from the labels of the containers, in the private scratch folder so no Compose file of
  the application's own folder is picked up. Recreate needs the files.
- Recreate is ``up -d --no-deps --force-recreate``: without ``--force-recreate`` Compose keeps a
  container whose configuration is unchanged, which is the case for a file a service reads
  through a mount and for any file its definition does not name, exactly what a deploy changes.
  It runs with a private Docker config that holds the registry logins, like a post-deploy
  command, so an image of a private registry can be pulled.
- The output of the command is bounded and scrubbed of every value of the deployment's files and
  of the registry logins before the tail of a failed run is shown. It is never logged and never
  stored: the audit entry holds the service, the exit code and a fixed reason only.
- What was kept of the deployment's containers is dropped after every attempt, so the Overview
  that refreshes next reads the new state.
"""

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.log import get_logger
from app.models import AuditResult, Deployment
from app.services.audit import AuditActor, record_actor_audit
from app.services.compose_config import ComposeScanError
from app.services.compose_project import is_compose_project_name
from app.services.compose_scan import ResolvedFiles, resolve_compose_files
from app.services.deploy import (
    LOCK_UNAVAILABLE_MESSAGE,
    LOCKED_MESSAGE,
    REGISTRY_LOGINS_FAILED_REASON,
    REGISTRY_UNREADABLE_REASON,
)
from app.services.deploy_lock import (
    DeployLockBusyError,
    DeployLockUnavailableError,
    hold_deploy_lock,
)
from app.services.docker_cli import DockerCli
from app.services.hook_process import BoundedOutput, run_process
from app.services.paths import load_allowed_paths
from app.services.registries import (
    RegistryLogin,
    RegistryTokenError,
    load_registry_logins,
    login_secrets,
)
from app.services.run_config import RunConfigError, private_docker_config
from app.services.scrub_values import load_scrubber
from app.services.stack_discovery import NOT_USABLE_REASON
from app.services.stack_runtime import (
    RuntimeState,
    StackService,
    forget_stack_runtime,
    load_stack_runtime,
)

log = get_logger("container_actions")

SERVICE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHORT_TIMEOUT_SECONDS = 60.0
RECREATE_TIMEOUT_SECONDS = 180.0
# Probes, reads, the lock and the file work around the command, which the browser waits for too.
REQUEST_MARGIN_SECONDS = 120
CONTROL_TIMEOUT_MS = (int(RECREATE_TIMEOUT_SECONDS) + REQUEST_MARGIN_SECONDS) * 1000
TAIL_CHARACTERS = 1200

NO_PROJECT_MESSAGE = (
    "This deployment has no Docker Compose project name, so its containers cannot be controlled."
)
NO_FILES_MESSAGE = "Recreate needs the Compose files of the deployment, and none are linked."
FILES_UNUSABLE_MESSAGE = "The linked Compose files cannot be used: "
NOT_STARTED_MESSAGE = "The docker command could not be started."
LOGINS_NOT_WRITTEN_MESSAGE = (
    "The command was not run because the registry logins could not be prepared for it."
)
OUTPUT_HIDDEN_MESSAGE = "Its output could not be protected, so it is not shown."


class ContainerAction(StrEnum):
    """What can be done to a service's container."""

    START = "start"
    STOP = "stop"
    RESTART = "restart"
    RECREATE = "recreate"

    @property
    def audit_action(self) -> str:
        """The name of the audit entry."""
        return f"container.{self.value}"

    @property
    def timeout(self) -> float:
        """The seconds the command may run."""
        if self is ContainerAction.RECREATE:
            return RECREATE_TIMEOUT_SECONDS
        return SHORT_TIMEOUT_SECONDS

    @property
    def past(self) -> str:
        """The past tense, for what was done."""
        return {"start": "started", "stop": "stopped", "restart": "restarted"}.get(
            self.value, "recreated"
        )


class UnknownServiceError(Exception):
    """The name is not a service of the deployment's Compose project that has a container."""


class ActionRefusedError(Exception):
    """The action cannot run now. The message is fixed text that is safe to show."""

    def __init__(self, message: str) -> None:
        """Keep the sentence to show."""
        super().__init__(message)
        self.message = message


class ActionStatus(StrEnum):
    """How an attempt ended: ``DONE``, ``FAILED`` (it ran and did not finish well) or
    ``REFUSED`` (it did not run)."""

    DONE = "done"
    FAILED = "failed"
    REFUSED = "refused"


@dataclass(frozen=True)
class ActionPlan:
    """An action that can run: the command and what the dialog tells about the service.

    ``arguments`` follow the docker executable; ``words`` is the command as shown, with
    ``docker`` for the executable. ``directory`` is None when the command runs on the project
    alone. ``stack`` holds the containers of the service as Docker reported them.
    """

    action: ContainerAction
    project: str
    service: str
    arguments: tuple[str, ...]
    directory: Path | None
    stack: StackService

    @property
    def words(self) -> tuple[str, ...]:
        """The command as shown to the user."""
        return ("docker", *self.arguments)

    @property
    def uses_files(self) -> bool:
        """Whether the command runs on the linked Compose files."""
        return self.directory is not None


@dataclass(frozen=True)
class ActionOutcome:
    """The result of an attempt, for the toast: ``title`` and ``message`` are fixed text and
    ``output`` is the scrubbed tail of a failed command (None otherwise)."""

    status: ActionStatus
    title: str
    message: str
    output: str | None = None
    exit_code: int | None = None


def is_service_name(name: str) -> bool:
    """Whether the text is shaped like a Compose service name, which is what may reach argv."""
    return SERVICE_NAME_PATTERN.fullmatch(name) is not None


def compose_arguments(
    action: ContainerAction, project: str, service: str, files: ResolvedFiles | None
) -> tuple[str, ...]:
    """The arguments of ``docker`` for the action (see the module text for the two forms)."""
    scope = ["-p", project]
    if files is not None:
        scope = [
            "--project-directory",
            str(files.project_directory),
            "-p",
            project,
            *[part for path in files.paths for part in ("-f", str(path))],
        ]
    if action is ContainerAction.RECREATE:
        verb = ["up", "-d", "--no-deps", "--force-recreate", service]
    else:
        verb = [action.value, service]
    return ("compose", *scope, *verb)


async def _resolve_files(
    db: AsyncSession, deployment: Deployment, action: ContainerAction
) -> ResolvedFiles | None:
    """The linked Compose files as real paths, None when none are linked (and the action does
    not need them). Raises ``ActionRefusedError`` when they no longer resolve."""
    linked = list(deployment.compose_files or ())
    if not linked:
        if action is ContainerAction.RECREATE:
            raise ActionRefusedError(NO_FILES_MESSAGE)
        return None
    allowed = await load_allowed_paths(db)
    try:
        return await asyncio.to_thread(resolve_compose_files, allowed, deployment.root_path, linked)
    except ComposeScanError as error:
        raise ActionRefusedError(FILES_UNUSABLE_MESSAGE + str(error)) from error


async def prepare_container_action(
    db: AsyncSession,
    cli: DockerCli,
    deployment: Deployment,
    action: ContainerAction,
    service: str,
    *,
    fresh: bool = False,
) -> ActionPlan:
    """Check that the action can run on the service and build its command.

    ``fresh`` asks Docker now (a deciding check) instead of taking the cached status and the
    cached read of the containers. Raises ``UnknownServiceError`` for a name that is not a
    service of the project with a container, and ``ActionRefusedError`` with the reason to show
    for a deployment with no project, Docker that is not usable and Compose files that cannot be
    used.
    """
    if not is_service_name(service):
        raise UnknownServiceError
    project = (deployment.compose_project or "").strip()
    if not project or not is_compose_project_name(project):
        raise ActionRefusedError(NO_PROJECT_MESSAGE)
    status = await cli.status(fresh=fresh)
    if not status.available:
        raise ActionRefusedError(status.reason or NOT_USABLE_REASON)
    if fresh:
        forget_stack_runtime(cli, deployment.id)
    runtime = await load_stack_runtime(db, cli, deployment)
    if runtime.state is RuntimeState.UNAVAILABLE:
        raise ActionRefusedError(runtime.reason or NOT_USABLE_REASON)
    stack = next((item for item in runtime.services if item.name == service), None)
    if stack is None or not stack.containers:
        raise UnknownServiceError
    files = await _resolve_files(db, deployment, action)
    return ActionPlan(
        action=action,
        project=project,
        service=service,
        arguments=compose_arguments(action, project, service, files),
        directory=files.project_directory if files is not None else None,
        stack=stack,
    )


def tail_of(text: str, limit: int = TAIL_CHARACTERS) -> str:
    """The end of the text, from the start of a line when the cut falls inside one."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    newline = cut.find("\n")
    return (cut[newline + 1 :] if 0 <= newline < limit - 1 else cut).strip()


@dataclass(frozen=True)
class _Ran:
    """How the command ended and what it printed (scrubbed, for a run that did not end well)."""

    exit_code: int | None
    timed_out: bool
    started: bool
    output: str | None
    hidden: bool = False


async def _load_logins(db: AsyncSession, crypto: CryptoService) -> tuple[RegistryLogin, ...]:
    """The registry logins a recreate may pull with. Raises ``ActionRefusedError``."""
    try:
        return await load_registry_logins(db, crypto)
    except RegistryTokenError as error:
        log.warning("a registry token could not be opened for %s", error.host)
        raise ActionRefusedError(REGISTRY_UNREADABLE_REASON) from error
    except Exception as error:
        log.error("registry logins could not be loaded: %s", type(error).__name__)
        raise ActionRefusedError(REGISTRY_LOGINS_FAILED_REASON) from error


async def _run_command(
    cli: DockerCli, plan: ActionPlan, environment: dict[str, str], output: BoundedOutput
) -> _Ran:
    """Run the docker command once under the deadline of the action."""
    executable = cli.executable
    if executable is None:
        return _Ran(None, False, False, None)
    try:
        result = await run_process(
            [executable, *plan.arguments],
            cwd=plan.directory or cli.scratch,
            env=environment,
            timeout=plan.action.timeout,
            output=output,
        )
    except OSError:
        return _Ran(None, False, False, None)
    return _Ran(result.exit_code, result.timed_out, True, None)


async def _execute(
    db: AsyncSession,
    crypto: CryptoService,
    cli: DockerCli,
    deployment: Deployment,
    plan: ActionPlan,
) -> _Ran:
    """Run the command, with the private Docker config of the registry logins for a recreate,
    and scrub the tail of the output of a run that did not end well. Raises
    ``ActionRefusedError`` when the logins cannot be opened or written."""
    logins = await _load_logins(db, crypto) if plan.action is ContainerAction.RECREATE else ()
    output = BoundedOutput()
    if logins:
        try:
            with private_docker_config(cli.scratch, logins) as folder:
                environment = {**cli.compose_environment(), "DOCKER_CONFIG": str(folder)}
                ran = await _run_command(cli, plan, environment, output)
        except RunConfigError as error:
            raise ActionRefusedError(LOGINS_NOT_WRITTEN_MESSAGE) from error
    else:
        ran = await _run_command(cli, plan, cli.compose_environment(), output)
    if ran.exit_code == 0 or not ran.started:
        return ran
    try:
        scrubber = await load_scrubber(db, crypto, deployment.id, extra=login_secrets(logins))
    except Exception as error:
        log.error("output scrubbing values could not be loaded: %s", type(error).__name__)
        return _Ran(ran.exit_code, ran.timed_out, True, None, hidden=True)
    return _Ran(ran.exit_code, ran.timed_out, True, tail_of(output.text(scrubber)) or None)


def _outcome(plan: ActionPlan, ran: _Ran) -> ActionOutcome:
    """The toast of a command that was started or could not start."""
    action, service = plan.action, plan.service
    if ran.exit_code == 0:
        return ActionOutcome(
            ActionStatus.DONE,
            f"{service} {action.past}",
            f"The {service} container of {plan.project} was {action.past}.",
            exit_code=0,
        )
    if not ran.started:
        reason = NOT_STARTED_MESSAGE
    elif ran.timed_out:
        reason = f"Timed out after {int(action.timeout)} seconds and was stopped."
    else:
        reason = f"docker compose exited with code {ran.exit_code}."
    if ran.hidden:
        reason += " " + OUTPUT_HIDDEN_MESSAGE
    return ActionOutcome(
        ActionStatus.FAILED,
        f"{action.value.capitalize()} of {service} failed",
        reason,
        output=ran.output,
        exit_code=ran.exit_code,
    )


async def _audit(
    db: AsyncSession,
    actor: AuditActor,
    deployment: Deployment,
    action: ContainerAction,
    service: str,
    result: AuditResult,
    details: dict[str, object],
) -> None:
    """Record the attempt on the deployment and commit. The details hold no output."""
    await record_actor_audit(
        db,
        actor,
        action=action.audit_action,
        result=result,
        target_type="deployment",
        target_id=deployment.id,
        target_name=deployment.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        details={"service": service, "project": deployment.compose_project, **details},
    )
    await db.commit()


async def run_container_action(
    db: AsyncSession,
    crypto: CryptoService,
    cli: DockerCli,
    lock_dir: Path,
    deployment: Deployment,
    action: ContainerAction,
    service: str,
    *,
    actor: AuditActor,
) -> ActionOutcome:
    """Run the action on the service, audit the attempt and say how it ended.

    Everything is decided again now (``prepare_container_action`` with ``fresh``). Raises
    ``UnknownServiceError`` for a service that is not in the project; every other refusal is an
    outcome, audited as blocked. A command that failed or timed out is audited as a failure with
    its exit code.
    """

    async def refused(message: str) -> ActionOutcome:
        reason: dict[str, object] = {"reason": message}
        await _audit(db, actor, deployment, action, service, AuditResult.BLOCKED, reason)
        return ActionOutcome(
            ActionStatus.REFUSED, f"{action.value.capitalize()} of {service} not run", message
        )

    try:
        plan = await prepare_container_action(db, cli, deployment, action, service, fresh=True)
    except ActionRefusedError as error:
        return await refused(error.message)
    try:
        with hold_deploy_lock(lock_dir, deployment.root_path):
            ran = await _execute(db, crypto, cli, deployment, plan)
    except DeployLockBusyError:
        return await refused(LOCKED_MESSAGE)
    except DeployLockUnavailableError:
        return await refused(LOCK_UNAVAILABLE_MESSAGE)
    except ActionRefusedError as error:
        return await refused(error.message)
    finally:
        forget_stack_runtime(cli, deployment.id)
    outcome = _outcome(plan, ran)
    log.info(
        "container %s of service %s in deployment %s ended: %s",
        action.value,
        service,
        deployment.id,
        outcome.status.value,
    )
    details: dict[str, object] = {"exit_code": ran.exit_code}
    if ran.timed_out:
        details["timed_out"] = True
    await _audit(
        db,
        actor,
        deployment,
        action,
        service,
        AuditResult.SUCCESS if outcome.status is ActionStatus.DONE else AuditResult.FAILURE,
        details,
    )
    return outcome
