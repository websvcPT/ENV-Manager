"""Helpers for tests of the stack runtime: a fake ``docker`` that answers the four reads of
``stack_runtime_reads`` with canned documents, and builders of those documents."""

import json
from pathlib import Path

from app.tests.fake_docker import FakeDocker

BEHAVIOR = """case "$*" in
  "compose -p "*" ps --all --format json")
    cat "$dir/ps.out"; echo "secret detail" >&2; exit "$(cat "$dir/ps.code")" ;;
  "inspect --type container --format "*)
    cat "$dir/inspect.out"; echo "secret detail" >&2; exit "$(cat "$dir/inspect.code")" ;;
  "network ls "*)
    cat "$dir/network_ls.out"; echo "secret detail" >&2; exit "$(cat "$dir/network_ls.code")" ;;
  "network inspect "*)
    cat "$dir/network_inspect.out"; echo "secret detail" >&2
    exit "$(cat "$dir/network_inspect.code")" ;;
esac
"""
ZERO_TIME = "0001-01-01T00:00:00Z"
CREATED = "2026-10-27T10:00:00.123456789Z"
STARTED = "2026-10-27T10:00:05.5Z"


def container_id(number: int) -> str:
    """A full container id."""
    return f"{number:064x}"


def serve(
    docker: FakeDocker,
    *,
    ps: object = "",
    inspect: list[dict[str, object]] | str = "",
    network_ls: list[str] | str = "",
    network_inspect: list[dict[str, object]] | str = "",
    fail: str | None = None,
) -> None:
    """Make the fake answer the four reads; ``fail`` names the one that exits non-zero.

    ``ps`` is a list of entries printed one per line (a string is printed as it is, and a
    ``("array", entries)`` pair as one JSON array). The inspect and network documents are printed
    one per line.
    """
    if isinstance(ps, tuple):
        ps_text = json.dumps(ps[1])
    elif isinstance(ps, str):
        ps_text = ps
    else:
        assert isinstance(ps, list)
        ps_text = "".join(json.dumps(entry) + "\n" for entry in ps)
    files = {
        "ps": ps_text,
        "inspect": _lines(inspect),
        "network_ls": _lines([{"Name": name} for name in network_ls])
        if isinstance(network_ls, list)
        else network_ls,
        "network_inspect": _lines(network_inspect),
    }
    for name, text in files.items():
        (docker.folder / f"{name}.out").write_text(text, encoding="utf-8")
        (docker.folder / f"{name}.code").write_text("1" if name == fail else "0", encoding="utf-8")
    docker.behavior(BEHAVIOR)


def _lines(documents: list[dict[str, object]] | str) -> str:
    """The documents as one JSON text per line, or the text as given."""
    if isinstance(documents, str):
        return documents
    return "".join(json.dumps(document) + "\n" for document in documents)


def publisher(
    host_ip: str, host_port: int, target: int, protocol: str = "tcp"
) -> dict[str, object]:
    """One entry of the ``Publishers`` of a ``compose ps`` container."""
    return {
        "URL": host_ip,
        "TargetPort": target,
        "PublishedPort": host_port,
        "Protocol": protocol,
    }


def ps_entry(
    service: str,
    number: int,
    state: str = "running",
    *,
    health: str = "",
    exit_code: int = 0,
    image: str = "alpine:3.24.2",
    publishers: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    """One container as ``docker compose ps --format json`` prints it."""
    return {
        "ID": container_id(number),
        "Name": f"demo-{service}-{number}",
        "Service": service,
        "State": state,
        "Health": health,
        "ExitCode": exit_code,
        "Image": image,
        "Publishers": list(publishers),
    }


def attached(
    name: str, ip: str = "172.20.0.2", aliases: tuple[str, ...] = ("api",), ipv6: str = ""
) -> dict[str, object]:
    """One network of an inspected container."""
    return {"name": name, "ip": ip, "ipv6": ipv6, "aliases": list(aliases)}


def inspect_entry(
    number: int,
    *,
    status: str = "running",
    health: str | None = None,
    started: str = STARTED,
    finished: str = ZERO_TIME,
    exit_code: int = 0,
    restarts: int = 0,
    created: str = CREATED,
    networks: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    """One container as the inspect template prints it, closing ``null`` included."""
    return {
        "id": container_id(number),
        "status": status,
        "health": health,
        "started": started,
        "finished": finished,
        "exit": exit_code,
        "restarts": restarts,
        "created": created,
        "networks": [*networks, None],
    }


def network_entry(
    name: str,
    *,
    driver: str = "bridge",
    internal: bool = False,
    subnets: tuple[tuple[str, str], ...] = (),
) -> dict[str, object]:
    """One network as the network template prints it, closing ``null`` included."""
    return {
        "name": name,
        "driver": driver,
        "internal": internal,
        "subnets": [{"subnet": subnet, "gateway": gateway} for subnet, gateway in subnets] + [None],
    }


def recorded(docker: FakeDocker) -> list[tuple[str, ...]]:
    """The arguments of the recorded calls."""
    return [call.arguments for call in docker.calls()]


def make_fake(folder: Path) -> FakeDocker:
    """A fake docker that answers an empty project."""
    docker = FakeDocker(folder)
    serve(docker)
    return docker


LOGS_CASE = 'case "$*" in\n  *" logs "*) {} ;;\nesac\n'


def log_row(prefix: str, stamp: str, message: str) -> str:
    """One line as ``docker compose logs --timestamps --no-color`` prints it."""
    return f"{prefix}  | {stamp} {message}\n"


def serve_logs(docker: FakeDocker, output: str = "") -> None:
    """Answer the reads of a stack with the services ``mysql`` and ``web`` and make
    ``docker compose logs`` print ``output`` (which can be changed with ``say_logs``)."""
    serve(
        docker,
        ps=[ps_entry("mysql", 1), ps_entry("web", 2)],
        inspect=[inspect_entry(1), inspect_entry(2)],
        network_ls=[],
        network_inspect=[],
    )
    docker.behavior(BEHAVIOR + LOGS_CASE.format('cat "$dir/logs.out"'))
    say_logs(docker, output)


def say_logs(docker: FakeDocker, output: str) -> None:
    """Change what ``docker compose logs`` prints."""
    (docker.folder / "logs.out").write_text(output, encoding="utf-8")
