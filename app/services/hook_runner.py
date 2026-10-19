"""The runner of post-deploy commands: docker commands run without a shell after a deploy.

``DockerHookRunner`` implements the ``HookRunner`` seam of the deploy engine. It decides, in
order: no stored command means nothing to do; nothing changed (and run only if changed is on and
the deploy is not forced) skips; a Docker socket, daemon or compose plugin that is not usable
skips with the reason and leaves the deploy successful; a stored command or working directory
that no longer passes validation (checked again here, not only when saved) fails without running
anything. Otherwise the commands run in order in the working directory, the first non-zero exit
stops the rest, and one deadline covers all of them, so the deploy lock is never held longer than
the timeout. Reasons and output never contain the text of a command, which only the superadmin
may read.

A non-zero exit or a timeout is an expected outcome: it is returned as a failed result and logged
below error level, so error reporting is not triggered.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.core.log import get_logger
from app.models import PostDeployStatus
from app.services.docker_cli import DockerCli
from app.services.hook_commands import Command, CommandError, parse_commands
from app.services.hook_process import BoundedOutput, run_process
from app.services.hooks import PostDeployHook, PostDeployRequest, PostDeployResult
from app.services.paths import AllowedPaths, PathValidationError, normalize_path

log = get_logger("hook_runner")

NOT_CHANGED_REASON = "No file changed and the command runs only when one did."
REFUSED_REASON = "The command was not run because its settings are no longer valid."
NOT_STARTED_REASON = "The command could not be started."
OUTSIDE_PATHS_MESSAGE = "The working directory is outside the allowed paths."
NOT_A_FOLDER_MESSAGE = "The working directory does not exist or is not a folder."
ROOT_OUTSIDE_MESSAGE = "The deployment is outside the allowed paths."
DOCKER_UNAVAILABLE_REASON = "Docker is not available."
DEFAULT_TIMEOUT_SECONDS = 120


class WorkingDirectoryError(ValueError):
    """A working directory cannot be used; the message is safe to show."""


def resolve_working_directory(
    allowed: AllowedPaths, deployment_root: Path, configured: str | None
) -> Path:
    """The real working directory: the configured path, else the deployment root.

    The path is resolved now and must lie inside the allowed paths and be a folder. Raises
    ``WorkingDirectoryError``.
    """
    text = (configured or "").strip()
    try:
        directory = allowed.resolve(normalize_path(text) if text else deployment_root)
    except PathValidationError as error:
        raise WorkingDirectoryError(OUTSIDE_PATHS_MESSAGE) from error
    if not directory.is_dir():
        raise WorkingDirectoryError(NOT_A_FOLDER_MESSAGE)
    return directory


@dataclass(frozen=True)
class _Plan:
    """The validated commands and the working directory they run in."""

    commands: tuple[Command, ...]
    directory: Path


def _prepare(request: PostDeployRequest, hook: PostDeployHook) -> _Plan:
    """Validate the stored settings against the current state of the paths.

    Raises ``CommandError``, ``WorkingDirectoryError`` or ``PathValidationError``.
    """
    commands = parse_commands(hook.commands)
    root = request.allowed.resolve(request.deployment_root)
    directory = resolve_working_directory(request.allowed, root, hook.working_directory)
    return _Plan(commands, directory)


def _skipped(reason: str) -> PostDeployResult:
    """A skipped result with its reason."""
    return PostDeployResult(PostDeployStatus.SKIPPED, reason=reason)


def _refused(detail: str) -> PostDeployResult:
    """A failed result for settings that did not pass validation at run time."""
    return PostDeployResult(PostDeployStatus.FAILED, reason=f"{REFUSED_REASON} {detail}")


class DockerHookRunner:
    """Runs the stored post-deploy commands through the docker CLI of the app image."""

    def __init__(self, cli: DockerCli | None = None) -> None:
        """Use the given docker CLI, by default the one of the image and the standard socket."""
        self.cli = cli or DockerCli()

    async def run_post_deploy(self, request: PostDeployRequest) -> PostDeployResult:
        """Run the commands once, or report why they were skipped or that there are none."""
        hook = request.hook
        if hook is None or not hook.commands.strip():
            return PostDeployResult(PostDeployStatus.NONE)
        if hook.run_only_if_changed and not (request.changed or request.force):
            return _skipped(NOT_CHANGED_REASON)
        status = await self.cli.status(fresh=True)
        executable = self.cli.executable
        if not status.available or executable is None:
            return _skipped(status.reason or DOCKER_UNAVAILABLE_REASON)
        try:
            plan = _prepare(request, hook)
        except PathValidationError:
            return _refused(ROOT_OUTSIDE_MESSAGE)
        except (CommandError, WorkingDirectoryError) as error:
            return _refused(str(error))
        return await self._execute(request, hook, plan, executable)

    async def _execute(
        self, request: PostDeployRequest, hook: PostDeployHook, plan: _Plan, executable: str
    ) -> PostDeployResult:
        """Run the commands in order under one deadline and stop at the first failure."""
        timeout = max(hook.timeout_seconds or DEFAULT_TIMEOUT_SECONDS, 1)
        output = BoundedOutput()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        total = len(plan.commands)
        for number, command in enumerate(plan.commands, start=1):
            if total > 1:
                output.note(f"[command {number} of {total}]\n")
            try:
                result = await run_process(
                    [executable, *command[1:]],
                    cwd=plan.directory,
                    env=self.cli.environment(),
                    timeout=max(deadline - loop.time(), 0.0),
                    output=output,
                )
            except OSError:
                return self._failed(request, output, None, NOT_STARTED_REASON)
            if result.timed_out:
                reason = f"Timed out after {timeout} seconds and was stopped."
                return self._failed(request, output, None, reason)
            if result.exit_code != 0:
                subject = f"Command {number} of {total}" if total > 1 else "The command"
                reason = f"{subject} exited with code {result.exit_code}."
                return self._failed(request, output, result.exit_code, reason)
        log.info("post-deploy command of service %s ran", request.service_id)
        return PostDeployResult(
            PostDeployStatus.RAN, exit_code=0, output=output.text(request.scrubber) or None
        )

    @staticmethod
    def _failed(
        request: PostDeployRequest, output: BoundedOutput, exit_code: int | None, reason: str
    ) -> PostDeployResult:
        """A failed result. Logged as a warning: a failing command is not an application error."""
        log.warning("post-deploy command of service %s failed", request.service_id)
        return PostDeployResult(
            PostDeployStatus.FAILED,
            exit_code=exit_code,
            output=output.text(request.scrubber) or None,
            reason=reason,
        )
