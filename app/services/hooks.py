"""The post-deploy hook seam of the deploy engine.

The engine calls a ``HookRunner`` once per real deploy after every selected environment was
written or found unchanged, while it still holds the deploy lock. The runner decides everything
about the command itself (availability, run only if changed, execution, timeout, output); the
engine only records the ``PostDeployResult`` it returns. ``app.services.hook_runner`` holds the
runner that executes the stored docker commands.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.models import PostDeployStatus
from app.services.output_scrub import OutputScrubber
from app.services.paths import AllowedPaths


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
    environment was written; ``force`` repeats the force flag of the deploy. ``allowed`` is the
    allowed path list loaded for this deploy, which a working directory must lie in.
    ``scrubber`` knows the configuration values that must not appear in the output; the engine
    removes them again from whatever a runner returns, a runner that keeps output uses it to
    protect values at the edges of the kept part as well.
    """

    service_id: int
    service_name: str
    deployment_id: int
    deployment_name: str
    deployment_root: Path
    hook: PostDeployHook | None
    changed: bool
    force: bool
    allowed: AllowedPaths
    scrubber: OutputScrubber = field(default_factory=OutputScrubber)


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
