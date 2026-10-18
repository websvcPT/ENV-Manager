"""The post-deploy hook seam of the deploy engine.

The engine calls a ``HookRunner`` once per real deploy after every selected environment was
written or found unchanged, while it still holds the deploy lock. The runner decides everything
about the command itself (availability, run only if changed, execution, timeout, output); the
engine only records the ``PostDeployResult`` it returns. ``UnavailableHookRunner`` is the stand-in
until commands can run: a service with a stored command reports a skip with a reason.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.models import PostDeployStatus

COMMANDS_UNAVAILABLE_REASON = "Post-deploy commands are not available yet."


@dataclass(frozen=True)
class PostDeployHook:
    """The stored post-deploy command settings of a service."""

    commands: str
    working_directory: str | None
    timeout_seconds: int
    run_only_if_changed: bool


@dataclass(frozen=True)
class PostDeployRequest:
    """Everything a runner needs: the service, its real deployment root and what the deploy did.

    ``hook`` is None when the service has no command. ``changed`` is True when at least one
    environment was written; ``force`` repeats the force flag of the deploy.
    """

    service_id: int
    service_name: str
    deployment_id: int
    deployment_name: str
    deployment_root: Path
    hook: PostDeployHook | None
    changed: bool
    force: bool


@dataclass(frozen=True)
class PostDeployResult:
    """What happened to the command: status, exit code, truncated output and a reason."""

    status: PostDeployStatus
    exit_code: int | None = None
    output: str | None = None
    reason: str | None = None


class HookRunner(Protocol):
    """Runs the post-deploy command of a service."""

    async def run_post_deploy(self, request: PostDeployRequest) -> PostDeployResult:
        """Run the command once, or report why it was skipped or that there is none."""
        ...


class UnavailableHookRunner:
    """The runner used while commands cannot run: none without a command, else a skip."""

    async def run_post_deploy(self, request: PostDeployRequest) -> PostDeployResult:
        """Report no command for a service without one, otherwise a skip with the reason."""
        if request.hook is None or not request.hook.commands.strip():
            return PostDeployResult(PostDeployStatus.NONE)
        return PostDeployResult(PostDeployStatus.SKIPPED, reason=COMMANDS_UNAVAILABLE_REASON)
