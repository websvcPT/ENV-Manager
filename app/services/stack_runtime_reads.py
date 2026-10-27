"""Reading the containers and networks of a Compose project from Docker, field by field.

Three reads, all through ``run_docker_query``:

- ``docker compose -p <project> ps --all --format json`` names the containers with their service,
  state, health, exit code, image and published ports. Compose prints either one JSON object per
  line or one JSON array, depending on its version; both are read, and an entry that is not an
  object with an id and a service is skipped.
- ``docker inspect`` with ``CONTAINER_TEMPLATE`` adds the times, the restart count and the
  addresses on each network.
- ``docker network ls`` with the project label and ``docker network inspect`` with
  ``NETWORK_TEMPLATE`` describe the networks of the project and the other networks its containers
  are attached to.

The templates select single fields and print one JSON document per line. They never read
``.Config`` (which holds the environment values of a container), the health probe output
(``.State.Health.Log``), the container list of a network or any whole object, so no secret can
reach this module. Nothing a failing command printed is passed on: failures are the fixed
sentences of ``docker_query`` or ``UNREADABLE_REASON``.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.services.docker_cli import DockerCli
from app.services.docker_query import run_docker_query

PROJECT_LABEL = "com.docker.compose.project"
UNREADABLE_REASON = "Docker gave an answer that could not be read."
MAX_CONTAINERS = 500
MAX_NETWORKS = 200

# Every segment is a single field. The health is read through ``index`` because the key is absent
# without a healthcheck and Docker refuses a template that names a missing key. The trailing
# ``null`` of a list ends the comma after its last element, and the readers drop it.
CONTAINER_TEMPLATE = (
    '{"id":{{json .Id}},'
    '"status":{{json .State.Status}},'
    '"health":{{if index .State "Health"}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"started":{{json .State.StartedAt}},'
    '"finished":{{json .State.FinishedAt}},'
    '"exit":{{json .State.ExitCode}},'
    '"restarts":{{json .RestartCount}},'
    '"created":{{json .Created}},'
    '"networks":[{{range $name, $network := .NetworkSettings.Networks}}'
    '{"name":{{json $name}},'
    '"ip":{{json $network.IPAddress}},'
    '"ipv6":{{json $network.GlobalIPv6Address}},'
    '"aliases":{{json $network.Aliases}}},'
    "{{end}}null]}"
)
NETWORK_TEMPLATE = (
    '{"name":{{json .Name}},'
    '"driver":{{json .Driver}},'
    '"internal":{{json .Internal}},'
    '"subnets":[{{range .IPAM.Config}}'
    '{"subnet":{{json .Subnet}},"gateway":{{json .Gateway}}},'
    "{{end}}null]}"
)

_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
_FRACTION = re.compile(r"(\.\d{1,6})\d*")
_ALL_INTERFACES = frozenset({"", "0.0.0.0", "::"})  # noqa: S104 - names of binds, nothing binds
_THIS_HOST = frozenset({"127.0.0.1", "::1"})


class UnreadableAnswer(Exception):
    """Docker answered, but not with anything this module can read."""


class PortBind(StrEnum):
    """Which interfaces a published port listens on."""

    ALL_INTERFACES = "all_interfaces"
    THIS_HOST = "this_host"
    ADDRESS = "address"


@dataclass(frozen=True)
class PublishedPort:
    """A container port published on the host: the host address and port, the container port."""

    host_ip: str
    host_port: int
    container_port: int
    protocol: str

    @property
    def bind(self) -> PortBind:
        """All interfaces (``0.0.0.0``, ``::`` or no address), this host only (loopback) or one
        other address."""
        if self.host_ip in _ALL_INTERFACES:
            return PortBind.ALL_INTERFACES
        if self.host_ip in _THIS_HOST:
            return PortBind.THIS_HOST
        return PortBind.ADDRESS


@dataclass(frozen=True)
class ContainerNetwork:
    """A container's place on one network."""

    name: str
    ip_address: str
    ipv6_address: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ContainerRecord:
    """What Docker says about one container of the project.

    ``started_at`` and ``finished_at`` are None when the container never started or never
    stopped. ``uptime`` and ``recreate_to_apply`` are filled by the caller that knows the time
    and the deploy history.
    """

    container_id: str
    name: str
    service: str
    state: str
    health: str | None
    exit_code: int | None
    image: str
    ports: tuple[PublishedPort, ...]
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    restart_count: int | None = None
    networks: tuple[ContainerNetwork, ...] = ()
    uptime: timedelta | None = None
    recreate_to_apply: bool = False


@dataclass(frozen=True)
class Subnet:
    """One address range of a network and its gateway."""

    subnet: str
    gateway: str


@dataclass(frozen=True)
class NetworkRecord:
    """A network the containers use. ``external`` is a network that does not belong to the
    project (declared external, or shared with other stacks)."""

    name: str
    driver: str
    internal: bool
    subnets: tuple[Subnet, ...]
    external: bool


@dataclass(frozen=True)
class DockerSnapshot:
    """The containers and networks of a project as read at one moment."""

    containers: tuple[ContainerRecord, ...]
    networks: tuple[NetworkRecord, ...]


def _text(value: object) -> str:
    """The value when it is a string, else an empty string."""
    return value if isinstance(value, str) else ""


def _number(value: object) -> int | None:
    """The value when it is an integer (a boolean is not one), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_time(value: object) -> datetime | None:
    """An RFC 3339 time of Docker as an aware UTC datetime; None for text that is not one and for
    the zero time Docker prints for a container that never started or never stopped."""
    if not isinstance(value, str) or not value:
        return None
    text = _FRACTION.sub(r"\1", value.strip())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return None if parsed.year <= 1 else parsed


def _json_objects(output: str) -> tuple[list[object], bool]:
    """The JSON values of an output that holds one array or one value per line, and whether any
    text could not be read. A blank output holds none and is not a failure."""
    text = output.strip()
    if not text:
        return [], False
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return [], True
        return (loaded if isinstance(loaded, list) else []), not isinstance(loaded, list)
    values: list[object] = []
    skipped = False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            skipped = True
    return values, skipped


def _dicts(values: Iterable[object]) -> list[dict[str, object]]:
    """The entries that are JSON objects."""
    return [value for value in values if isinstance(value, dict)]


def _published_ports(entry: dict[str, object]) -> tuple[PublishedPort, ...]:
    """The ports of one ``ps`` entry that are published on the host (an exposed port has a host
    port of 0 and is left out)."""
    publishers = entry.get("Publishers")
    ports: list[PublishedPort] = []
    for publisher in _dicts(publishers if isinstance(publishers, list) else []):
        host_port = _number(publisher.get("PublishedPort"))
        container_port = _number(publisher.get("TargetPort"))
        if not host_port or container_port is None:
            continue
        ports.append(
            PublishedPort(
                _text(publisher.get("URL")),
                host_port,
                container_port,
                _text(publisher.get("Protocol")) or "tcp",
            )
        )
    return tuple(ports)


def parse_containers(output: str) -> tuple[ContainerRecord, ...]:
    """The containers of a ``compose ps --format json`` output, sorted by service and name.

    Raises ``UnreadableAnswer`` when the output holds text but not one container. Lines and
    entries that are not containers are skipped.
    """
    values, unreadable = _json_objects(output)
    containers: list[ContainerRecord] = []
    for entry in _dicts(values):
        container_id = _text(entry.get("ID"))
        service = _text(entry.get("Service"))
        if not _CONTAINER_ID.fullmatch(container_id) or not service:
            continue
        containers.append(
            ContainerRecord(
                container_id=container_id,
                name=_text(entry.get("Name")),
                service=service,
                state=_text(entry.get("State")),
                health=_text(entry.get("Health")) or None,
                exit_code=_number(entry.get("ExitCode")),
                image=_text(entry.get("Image")),
                ports=_published_ports(entry),
            )
        )
    if not containers and (unreadable or values):
        raise UnreadableAnswer
    return tuple(sorted(containers, key=lambda item: (item.service, item.name)))[:MAX_CONTAINERS]


def _container_networks(value: object) -> tuple[ContainerNetwork, ...]:
    """The networks of one inspected container; the closing ``null`` is dropped."""
    networks: list[ContainerNetwork] = []
    for entry in _dicts(value if isinstance(value, list) else []):
        name = _text(entry.get("name"))
        if not name:
            continue
        aliases = entry.get("aliases")
        networks.append(
            ContainerNetwork(
                name=name,
                ip_address=_text(entry.get("ip")),
                ipv6_address=_text(entry.get("ipv6")),
                aliases=tuple(
                    alias
                    for alias in (aliases if isinstance(aliases, list) else [])
                    if isinstance(alias, str)
                ),
            )
        )
    return tuple(sorted(networks, key=lambda item: item.name))


def _with_details(record: ContainerRecord, details: list[dict[str, object]]) -> ContainerRecord:
    """The record completed with the inspected details of the same container, if any."""
    found = next(
        (entry for entry in details if _text(entry.get("id")).startswith(record.container_id)),
        None,
    )
    if found is None:
        return record
    health = _text(found.get("health")) or record.health
    exit_code = _number(found.get("exit"))
    return ContainerRecord(
        container_id=record.container_id,
        name=record.name,
        service=record.service,
        state=_text(found.get("status")) or record.state,
        health=health,
        exit_code=record.exit_code if exit_code is None else exit_code,
        image=record.image,
        ports=record.ports,
        created_at=parse_time(found.get("created")),
        started_at=parse_time(found.get("started")),
        finished_at=parse_time(found.get("finished")),
        restart_count=_number(found.get("restarts")),
        networks=_container_networks(found.get("networks")),
    )


def parse_network_names(output: str) -> tuple[str, ...]:
    """The names in a ``docker network ls --format json`` output."""
    values, _ = _json_objects(output)
    return tuple(name for entry in _dicts(values) if (name := _text(entry.get("Name"))))


def _subnets(value: object) -> tuple[Subnet, ...]:
    """The address ranges of one inspected network; the closing ``null`` is dropped."""
    return tuple(
        Subnet(_text(entry.get("subnet")), _text(entry.get("gateway")))
        for entry in _dicts(value if isinstance(value, list) else [])
        if _text(entry.get("subnet"))
    )


def parse_networks(output: str, project_networks: frozenset[str]) -> tuple[NetworkRecord, ...]:
    """The networks of a ``docker network inspect`` output with the project's own first."""
    values, _ = _json_objects(output)
    networks = [
        NetworkRecord(
            name=name,
            driver=_text(entry.get("driver")),
            internal=entry.get("internal") is True,
            subnets=_subnets(entry.get("subnets")),
            external=name not in project_networks,
        )
        for entry in _dicts(values)
        if (name := _text(entry.get("name")))
    ]
    return tuple(sorted(networks, key=lambda item: (item.external, item.name)))


async def _read_details(
    cli: DockerCli, containers: tuple[ContainerRecord, ...]
) -> list[dict[str, object]]:
    """Inspect the containers once, with only the fields of ``CONTAINER_TEMPLATE``."""
    output = await run_docker_query(
        cli,
        [
            "inspect",
            "--type",
            "container",
            "--format",
            CONTAINER_TEMPLATE,
            *(record.container_id for record in containers),
        ],
    )
    values, _ = _json_objects(output)
    return _dicts(values)


async def _read_networks(
    cli: DockerCli, project: str, containers: tuple[ContainerRecord, ...]
) -> tuple[NetworkRecord, ...]:
    """The networks of the project and the others its containers are attached to."""
    listed = await run_docker_query(
        cli,
        [
            "network",
            "ls",
            "--filter",
            f"label={PROJECT_LABEL}={project}",
            "--format",
            "json",
        ],
    )
    own = frozenset(parse_network_names(listed))
    attached = {network.name for record in containers for network in record.networks}
    names = sorted(name for name in own | attached if not name.startswith("-"))[:MAX_NETWORKS]
    if not names:
        return ()
    inspected = await run_docker_query(
        cli, ["network", "inspect", "--format", NETWORK_TEMPLATE, *names]
    )
    return parse_networks(inspected, own)


async def read_snapshot(cli: DockerCli, project: str) -> DockerSnapshot:
    """The containers and networks of a Compose project, read with four docker commands at most.

    Raises ``DockerQueryError`` when a command fails and ``UnreadableAnswer`` when the list of
    containers holds text that is not a container.
    """
    listed = await run_docker_query(
        cli, ["compose", "-p", project, "ps", "--all", "--format", "json"]
    )
    containers = parse_containers(listed)
    if not containers:
        return DockerSnapshot((), ())
    details = await _read_details(cli, containers)
    detailed = tuple(_with_details(record, details) for record in containers)
    return DockerSnapshot(detailed, await _read_networks(cli, project, detailed))
