"""What the Overview shows of a deployment's containers, for one caller.

``build_runtime_view`` joins the runtime of the Compose project (``stack_runtime``) with the
services of the deployment detail the caller may see. The detail carries the files and deploy
columns and the visibility; the runtime carries the containers.

- The rows start from the services the caller sees, so a caller with access to some services
  only never gets a row, a container count or a network for the others. Compose services with no
  stored service (the stack has them, the application has no files for them) are rows for
  callers who see the whole deployment only.
- Without a readable runtime (no project, Docker not usable, a project with no containers) the
  rows are the stored services alone, without containers.
- The networks and the project name are part of the view for callers who see the whole
  deployment only.
"""

from dataclasses import dataclass
from datetime import datetime

from app.services.deployment_detail import DeploymentDetail, ServiceRow
from app.services.stack_runtime import RUNNING_STATE, RuntimeState, StackRuntime, StackService
from app.services.stack_runtime_reads import ContainerRecord, NetworkRecord


@dataclass(frozen=True)
class RuntimeRow:
    """One service of the Overview: its stored side (files, deploys) and its containers.

    ``stored`` is None for a Compose service the application has no files for, ``stack`` is None
    when the runtime could not be read.
    """

    name: str
    stored: ServiceRow | None
    stack: StackService | None

    @property
    def containers(self) -> tuple[ContainerRecord, ...]:
        """The containers of the service."""
        return self.stack.containers if self.stack else ()

    @property
    def images(self) -> tuple[str, ...]:
        """The distinct images of the containers, in order."""
        return tuple(dict.fromkeys(container.image for container in self.containers))

    @property
    def recreate_to_apply(self) -> bool:
        """Whether a container predates the last write of the service's files."""
        return bool(self.stack and self.stack.recreate_to_apply)

    @property
    def stale_created_at(self) -> datetime | None:
        """When the oldest container that must be recreated was created, None when none must."""
        created = [
            item.created_at
            for item in self.containers
            if item.recreate_to_apply and item.created_at is not None
        ]
        return min(created) if created else None


@dataclass(frozen=True)
class RuntimeView:
    """The runtime section of the Overview for one caller.

    ``full`` is whether the caller sees the whole deployment; ``project`` and ``networks`` are
    empty when not.
    The counts cover the containers of ``rows`` only.
    """

    full: bool
    state: RuntimeState
    reason: str | None
    project: str | None
    rows: tuple[RuntimeRow, ...]
    networks: tuple[NetworkRecord, ...]
    running: int
    total: int
    to_recreate: int

    @property
    def show_counts(self) -> bool:
        """Whether the status strip carries the Containers and Applied cells."""
        return self.state is RuntimeState.READY and self.total > 0

    @property
    def connections(self) -> tuple[tuple[RuntimeRow, ContainerRecord], ...]:
        """Each container that has an address or a published port, with its service row."""
        return tuple(
            (row, container)
            for row in self.rows
            for container in row.containers
            if container.networks or container.ports
        )

    @property
    def show_network(self) -> bool:
        """Whether there is something for the Network panel: a network, an address or a port."""
        if not self.full or self.state is not RuntimeState.READY:
            return False
        return bool(self.networks) or bool(self.connections)


def build_runtime_view(runtime: StackRuntime, detail: DeploymentDetail) -> RuntimeView:
    """The runtime as the caller of ``detail`` sees it."""
    stored = {row.id: row for row in detail.services}
    rows: tuple[RuntimeRow, ...]
    if runtime.state in (RuntimeState.READY, RuntimeState.EMPTY):
        shown: list[RuntimeRow] = []
        for item in runtime.services:
            own = stored.get(item.service_id) if item.service_id is not None else None
            if own is not None or detail.full:
                shown.append(RuntimeRow(item.name, own, item))
        rows = tuple(shown)
    else:
        rows = tuple(RuntimeRow(row.name, row, None) for row in detail.services)
    containers = [container for row in rows for container in row.containers]
    return RuntimeView(
        full=detail.full,
        state=runtime.state,
        reason=runtime.reason,
        project=runtime.project if detail.full else None,
        rows=rows,
        networks=runtime.networks if detail.full else (),
        running=sum(1 for container in containers if container.state == RUNNING_STATE),
        total=len(containers),
        to_recreate=sum(1 for container in containers if container.recreate_to_apply),
    )
