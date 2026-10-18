"""The deploy-finished notification seam.

The deploy engine emits one ``DeployFinished`` event for every real deploy that was recorded,
after the lock is released. Whatever delivers notifications implements ``DeployNotifier``; the
engine ignores a notifier that fails. The event carries ids, names and outcomes only: no values,
no file content, no command output.
"""

from dataclasses import dataclass
from typing import Protocol

from app.models import DeployResultStatus, DeployStatus, PostDeployStatus


@dataclass(frozen=True)
class EnvironmentResult:
    """The outcome of one environment in a finished deploy."""

    environment_id: int
    name: str
    version: int | None
    status: DeployResultStatus


@dataclass(frozen=True)
class DeployFinished:
    """A real deploy run that finished. ``actor`` is the label of the user, token or CLI."""

    run_id: int
    project_id: int
    deployment_id: int
    deployment_name: str
    service_id: int
    service_name: str
    actor: str
    force: bool
    status: DeployStatus
    post_deploy: PostDeployStatus
    environments: tuple[EnvironmentResult, ...]


class DeployNotifier(Protocol):
    """Receives deploy-finished events."""

    async def deploy_finished(self, event: DeployFinished) -> None:
        """Handle the event; the engine does not wait for anything but this call."""
        ...


class NoNotifier:
    """The notifier used until notifications exist: it does nothing."""

    async def deploy_finished(self, event: DeployFinished) -> None:
        """Ignore the event."""
