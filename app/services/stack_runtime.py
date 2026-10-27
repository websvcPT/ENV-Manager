"""What the containers of a deployment's Compose project are doing.

``load_stack_runtime`` is the read the Overview tab makes: the containers of the deployment's
Compose project with their state, health, uptime, restarts, published ports and addresses, the
networks they use, and how all of it lines up with the services stored in the application.

- No Compose project on the deployment, Docker not usable, a failing or unreadable answer and a
  project without containers are states of the result, never exceptions. The reason of an
  unusable Docker is the fixed text of ``DockerCli.status``, the one of a failing command the
  fixed sentence of ``docker_query``.
- Compose services and stored services are matched by name. A Compose service with no stored
  service has no files in the application (``StackService.has_files`` is False); a stored
  service with no container has ``no_container``. The deployment's own files are not a service
  here.
- A container reads its ``env_file`` values when it is created, so a restart does not apply a
  deploy. A container created before the files of its service were last written is marked
  ``recreate_to_apply``. Only a result that wrote a file counts, whatever the run ended as (the
  files are written one by one, so a halted or failed run can have written some): a dry run, an
  import from Compose and a result that found the file identical wrote nothing. The time compared
  is the one the result was recorded at, which is after the files were written and before the
  post-deploy command ran, so a container that command recreated is not marked.
- What Docker answered is kept for five seconds per deployment and project (the clock is
  injectable), by the docker command object like its status, so the periodic refresh of the tab
  and several viewers cost one set of docker commands. Deploy history and the match with the
  stored services are read on every call, and the uptime is computed from the time of the call.
"""

import asyncio
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployTrigger,
    Service,
)
from app.models.types import utc_now
from app.services.compose_project import is_compose_project_name
from app.services.docker_cli import DockerCli
from app.services.docker_query import DockerQueryError
from app.services.services import listed_service
from app.services.stack_discovery import NOT_USABLE_REASON
from app.services.stack_runtime_reads import (
    UNREADABLE_REASON,
    ContainerRecord,
    DockerSnapshot,
    NetworkRecord,
    UnreadableAnswer,
    read_snapshot,
)

CACHE_TTL_SECONDS = 5.0
RUNNING_STATE = "running"


class RuntimeState(StrEnum):
    """What the read found: ``NO_PROJECT`` (the deployment has no Compose project name),
    ``UNAVAILABLE`` (Docker could not be read, ``reason`` says why), ``EMPTY`` (the project has
    no containers) or ``READY``."""

    NO_PROJECT = "no_project"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"
    READY = "ready"


@dataclass(frozen=True)
class StackService:
    """One service of the stack, from Compose, from the application or from both.

    ``service_id`` is the stored service of the same name, None when there is none.
    ``last_written_at`` is when a deploy last wrote a file of the stored service.
    """

    name: str
    service_id: int | None
    containers: tuple[ContainerRecord, ...]
    last_written_at: datetime | None = None

    @property
    def has_files(self) -> bool:
        """Whether the application stores files for this service."""
        return self.service_id is not None

    @property
    def no_container(self) -> bool:
        """Whether the stored service has no container in the project."""
        return not self.containers

    @property
    def recreate_to_apply(self) -> bool:
        """Whether any container predates the last write of the service's files."""
        return any(container.recreate_to_apply for container in self.containers)


@dataclass(frozen=True)
class StackRuntime:
    """The runtime of a deployment's Compose project, or why it could not be read."""

    state: RuntimeState
    project: str | None = None
    reason: str | None = None
    services: tuple[StackService, ...] = ()
    networks: tuple[NetworkRecord, ...] = ()

    @property
    def containers(self) -> tuple[ContainerRecord, ...]:
        """Every container of the project, by service."""
        return tuple(container for service in self.services for container in service.containers)

    @property
    def running_count(self) -> int:
        """How many containers are running."""
        return sum(1 for container in self.containers if container.state == RUNNING_STATE)

    @property
    def recreate_count(self) -> int:
        """How many containers need to be recreated to apply deployed files."""
        return sum(1 for container in self.containers if container.recreate_to_apply)


@dataclass(frozen=True)
class _Reading:
    """What one set of docker commands gave: a snapshot, or the reason there is none."""

    snapshot: DockerSnapshot | None
    reason: str | None = None


class RuntimeCache:
    """Readings kept for ``CACHE_TTL_SECONDS`` per key, one read at a time per key.

    A second caller that arrives while a read is running waits for it and takes its result.
    Failures are kept too, so an unwell daemon is not asked again by every refresh.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl: float = CACHE_TTL_SECONDS
    ) -> None:
        """Keep readings for ``ttl`` seconds of ``clock``."""
        self._clock = clock
        self._ttl = ttl
        self._readings: dict[tuple[int, str], tuple[float, _Reading]] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._forgotten: dict[int, int] = {}

    async def get(self, key: tuple[int, str], read: Callable[[], Awaitable[_Reading]]) -> _Reading:
        """The reading of ``key``, made by ``read`` when there is none or it is too old."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            known = self._readings.get(key)
            if known is not None and self._clock() - known[0] < self._ttl:
                return known[1]
            forgotten = self._forgotten.get(key[0], 0)
            reading = await read()
            if self._forgotten.get(key[0], 0) == forgotten:
                self._readings[key] = (self._clock(), reading)
            return reading

    def forget(self, deployment_id: int) -> None:
        """Drop the readings of a deployment, so the next call asks Docker again.

        A read that was already running is not kept either: it may have started before the
        change that made the caller forget.
        """
        self._forgotten[deployment_id] = self._forgotten.get(deployment_id, 0) + 1
        for key in [key for key in self._readings if key[0] == deployment_id]:
            del self._readings[key]


_caches: weakref.WeakKeyDictionary[DockerCli, RuntimeCache] = weakref.WeakKeyDictionary()


def _cache_of(cli: DockerCli) -> RuntimeCache:
    """The cache that belongs to a docker command object."""
    cache = _caches.get(cli)
    if cache is None:
        cache = _caches[cli] = RuntimeCache()
    return cache


def forget_stack_runtime(cli: DockerCli, deployment_id: int) -> None:
    """Drop what was kept of a deployment's containers, after something changed them."""
    _cache_of(cli).forget(deployment_id)


async def _read(cli: DockerCli, project: str) -> _Reading:
    """Read the project, turning every failure into the reason to show."""
    try:
        return _Reading(await read_snapshot(cli, project))
    except DockerQueryError as error:
        return _Reading(None, error.message)
    except UnreadableAnswer:
        return _Reading(None, UNREADABLE_REASON)


async def _last_writes(db: AsyncSession, service_ids: set[int]) -> dict[int, datetime]:
    """When a deploy last wrote a file, per stored service that has one in its history."""
    if not service_ids:
        return {}
    rows = await db.execute(
        select(DeployRun.service_id, func.max(DeployResult.created_at))
        .join(DeployResult, DeployResult.run_id == DeployRun.id)
        .where(
            DeployRun.service_id.in_(service_ids),
            DeployRun.dry_run.is_(False),
            DeployRun.trigger != DeployTrigger.COMPOSE,
            DeployResult.status == DeployResultStatus.WRITTEN,
        )
        .group_by(DeployRun.service_id)
    )
    return {service_id: written for service_id, written in rows.all() if service_id is not None}


def _completed(
    container: ContainerRecord, written: datetime | None, now: datetime
) -> ContainerRecord:
    """The container with its uptime (running ones) and whether it predates the last write."""
    uptime = (
        max(now - container.started_at, timedelta())
        if container.state == RUNNING_STATE and container.started_at is not None
        else None
    )
    stale = (
        written is not None and container.created_at is not None and container.created_at < written
    )
    return replace(container, uptime=uptime, recreate_to_apply=stale)


async def _services(
    db: AsyncSession, deployment_id: int, containers: tuple[ContainerRecord, ...], now: datetime
) -> tuple[StackService, ...]:
    """Match the containers with the stored services, by name, one entry per name."""
    rows = await db.execute(
        select(Service.id, Service.name).where(
            Service.deployment_id == deployment_id, listed_service()
        )
    )
    stored = {name: service_id for service_id, name in rows.all()}
    written = await _last_writes(db, set(stored.values()))
    names = sorted(stored.keys() | {container.service for container in containers})
    services: list[StackService] = []
    for name in names:
        service_id = stored.get(name)
        last_written = written.get(service_id) if service_id is not None else None
        services.append(
            StackService(
                name=name,
                service_id=service_id,
                containers=tuple(
                    _completed(container, last_written, now)
                    for container in containers
                    if container.service == name
                ),
                last_written_at=last_written,
            )
        )
    return tuple(services)


async def load_stack_runtime(
    db: AsyncSession,
    cli: DockerCli,
    deployment: Deployment,
    *,
    now: datetime | None = None,
    cache: RuntimeCache | None = None,
) -> StackRuntime:
    """The runtime of the deployment's Compose project.

    ``now`` is the time uptimes are measured to (the current time by default). ``cache`` replaces
    the cache of the docker command object, which tests use to control the clock.
    """
    project = (deployment.compose_project or "").strip()
    if not project or not is_compose_project_name(project):
        return StackRuntime(RuntimeState.NO_PROJECT)
    status = await cli.status()
    if not status.available:
        return StackRuntime(RuntimeState.UNAVAILABLE, project, status.reason or NOT_USABLE_REASON)
    reading = await (cache or _cache_of(cli)).get(
        (deployment.id, project), lambda: _read(cli, project)
    )
    if reading.snapshot is None:
        return StackRuntime(RuntimeState.UNAVAILABLE, project, reading.reason)
    services = await _services(db, deployment.id, reading.snapshot.containers, now or utc_now())
    state = RuntimeState.READY if reading.snapshot.containers else RuntimeState.EMPTY
    return StackRuntime(state, project, None, services, reading.snapshot.networks)
