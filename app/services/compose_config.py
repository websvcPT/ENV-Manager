"""Reading the structure of Compose files with the real ``docker compose config``.

The command is ``docker compose --project-directory D -p envmanager-scan --profile '*' -f F...
config --format json --no-env-resolution -o FILE``, started without a shell, in the project
directory, with the minimal environment of ``DockerCli`` and a deadline. Checked against Docker
Compose v5.6.0 in the app image, on fixture projects:

- ``config`` needs no daemon and no socket: it runs with ``DOCKER_HOST`` pointing at a socket that
  does not exist, as in the development stack.
- ``--profile '*'`` keeps the services that only exist under a profile; without it they vanish.
- ``--no-env-resolution`` leaves each ``env_file`` entry as ``{"path": <absolute path>}`` (with
  ``"required": false`` only when optional) and does not read the files. Without it a missing
  required env file ends the command with exit code 1 and the files are merged into
  ``environment``.
- ``-o FILE`` keeps the document out of the output streams, where warnings (a variable that is
  not set) would be mixed into it. The document holds values interpolated from the project
  ``.env`` and the environment, so only the structure is read from it and the file is removed.
- Relative paths and the ``.env`` file resolve against the project directory, which is the folder
  of the base file (the Compose default), not the deployment root: ``--project-directory`` on the
  root gave other paths for a base file one folder below. Files of an ``include`` or an
  ``extends`` come out as absolute paths relative to the file that names them, overrides merge
  as Compose merges them, and the services are listed in alphabetical order.
- A git include (an ``https`` address ending in ``.git``, ``git@host:path`` or ``git://``) and an
  ``oci://`` include connect to the network. A plain ``https://x.yaml`` is read as a local path.
  ``COMPOSE_EXPERIMENTAL_GIT_REMOTE=false`` and ``COMPOSE_EXPERIMENTAL_OCI_REMOTE=false`` make
  Compose refuse them before any connection, which backs up the check of the parsed Compose files
  (``compose_includes``) that runs first.

Compose reads the files an ``include`` or an ``extends`` names wherever they are, and the
``env_file`` of a long form include. ``compose_includes`` parses the Compose files first and
refuses every such target that is remote, holds a variable (Compose expands it) or lies outside
the deployment root and the allowed paths, so the command never starts on one. The files and
paths such a file adds are then judged against the root and the allowed paths like any other.

Compose's own messages are never shown: they can quote file content, so a failure is reduced to a
fixed sentence (the name of a missing required variable is the one detail kept).
"""

import asyncio
import json
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.log import get_logger
from app.services.compose_includes import ComposeReferenceError, check_compose_references
from app.services.docker_cli import CLI_MISSING_REASON, DockerCli
from app.services.hook_process import BoundedOutput, run_process
from app.services.paths import AllowedPaths

log = get_logger("compose_config")

SCAN_TIMEOUT_SECONDS = 30.0
MAX_MODEL_BYTES = 8 * 1024 * 1024
MAX_COMPOSE_FILE_BYTES = 1024 * 1024
PROJECT_NAME = "envmanager-scan"

REMOTE_MESSAGE = (
    "A Compose file includes a remote source (git, http or OCI). "
    "Only local files are read, so nothing was scanned."
)
NOT_STARTED_MESSAGE = "The docker command could not be started."
TIMED_OUT_MESSAGE = "Docker Compose did not finish in time, so nothing was scanned."
YAML_MESSAGE = "A Compose file is not valid YAML or does not follow the Compose format."
MISSING_FILE_MESSAGE = "A file named by the Compose files (an include or an extends) is missing."
UNREADABLE_MESSAGE = "Docker Compose could not read the Compose files."
OUTPUT_MESSAGE = "Docker Compose gave an answer that could not be read."

_REQUIRED_VARIABLE = re.compile(r"required variable (\w+) is missing")
_YAML_ERROR = re.compile(r"go-yaml|(?:^|\s)yaml:")


class ComposeScanError(Exception):
    """The Compose files cannot be scanned; the message is fixed text that is safe to show."""


@dataclass(frozen=True)
class EnvFileRef:
    """One ``env_file`` entry of a service: the absolute path Compose printed and whether it is
    required."""

    path: str
    required: bool


@dataclass(frozen=True)
class BindMount:
    """One bind mount of a service: the absolute source on the host and the path in the
    container, empty when Compose printed none."""

    source: str
    target: str


@dataclass(frozen=True)
class ComposeService:
    """What the scan keeps of one service.

    ``binds`` are its bind mounts, ``secrets`` and ``configs`` the names of the top-level entries
    it uses.
    """

    name: str
    profiles: tuple[str, ...]
    env_files: tuple[EnvFileRef, ...]
    binds: tuple[BindMount, ...]
    secrets: tuple[str, ...]
    configs: tuple[str, ...]

    @property
    def bind_sources(self) -> tuple[str, ...]:
        """The absolute sources of its bind mounts."""
        return tuple(bind.source for bind in self.binds)


@dataclass(frozen=True)
class ComposeModel:
    """The structure of the merged Compose files, without any interpolated value.

    ``secret_files`` and ``config_files`` map the name of a top-level ``secrets`` or ``configs``
    entry that has a ``file`` to that file's absolute path. ``services`` are in the alphabetical
    order Compose prints.
    """

    services: tuple[ComposeService, ...]
    secret_files: dict[str, str]
    config_files: dict[str, str]


def _text_of(value: object) -> str | None:
    """The value when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _entries(value: object) -> list[object]:
    """The value as a list, empty when it is not one."""
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict[str, object]:
    """The value as a mapping, empty when it is not one."""
    return value if isinstance(value, dict) else {}


def _env_files(service: dict[str, object]) -> tuple[EnvFileRef, ...]:
    """The ``env_file`` entries of a service, in the long form and as plain paths."""
    found: list[EnvFileRef] = []
    for entry in _entries(service.get("env_file")):
        if isinstance(entry, str) and entry:
            found.append(EnvFileRef(entry, True))
        elif isinstance(entry, dict) and (path := _text_of(entry.get("path"))):
            found.append(EnvFileRef(path, entry.get("required") is not False))
    return tuple(found)


def _binds(service: dict[str, object]) -> tuple[BindMount, ...]:
    """The bind mounts of a service, with their source and container path."""
    binds: list[BindMount] = []
    for entry in _entries(service.get("volumes")):
        volume = _mapping(entry)
        if volume.get("type") == "bind" and (source := _text_of(volume.get("source"))):
            binds.append(BindMount(source, _text_of(volume.get("target")) or ""))
    return tuple(binds)


def _used_names(service: dict[str, object], key: str) -> tuple[str, ...]:
    """The ``source`` names of the secrets or configs a service uses, as plain names or maps."""
    names: list[str] = []
    for entry in _entries(service.get(key)):
        name = entry if isinstance(entry, str) else _mapping(entry).get("source")
        if text := _text_of(name):
            names.append(text)
    return tuple(names)


def _top_level_files(model: dict[str, object], key: str) -> dict[str, str]:
    """The absolute ``file`` of each top-level secret or config that has one."""
    files: dict[str, str] = {}
    for name, entry in _mapping(model.get(key)).items():
        if path := _text_of(_mapping(entry).get("file")):
            files[name] = path
    return files


def extract_model(document: object) -> ComposeModel:
    """The structure kept from the JSON document of ``docker compose config``.

    Everything else in it (images, ports, ``environment`` with its interpolated values) is
    dropped here and never leaves this function.
    """
    model = _mapping(document)
    services: list[ComposeService] = []
    for name, raw in _mapping(model.get("services")).items():
        service = _mapping(raw)
        profiles = tuple(
            item for item in _entries(service.get("profiles")) if isinstance(item, str)
        )
        services.append(
            ComposeService(
                name=name,
                profiles=profiles,
                env_files=_env_files(service),
                binds=_binds(service),
                secrets=_used_names(service, "secrets"),
                configs=_used_names(service, "configs"),
            )
        )
    return ComposeModel(
        services=tuple(services),
        secret_files=_top_level_files(model, "secrets"),
        config_files=_top_level_files(model, "configs"),
    )


def failure_message(output: str) -> str:
    """The fixed sentence for a failed ``config`` run, from what Compose printed."""
    if "remote resource is disabled" in output or "OCI resource" in output:
        return REMOTE_MESSAGE
    if "failed to access repository" in output:
        return REMOTE_MESSAGE
    if (variable := _REQUIRED_VARIABLE.search(output)) is not None:
        return (
            f"The Compose files need the variable {variable.group(1)}, which is not set in the "
            "deployment environment file (.env)."
        )
    if "no such file or directory" in output:
        return MISSING_FILE_MESSAGE
    if _YAML_ERROR.search(output) or "validating" in output:
        return YAML_MESSAGE
    return UNREADABLE_MESSAGE


def _child_environment(cli: DockerCli) -> dict[str, str]:
    """The environment of the scan: the fixed one of ``DockerCli`` with remote sources off."""
    return {
        **cli.environment(),
        "COMPOSE_EXPERIMENTAL_GIT_REMOTE": "false",
        "COMPOSE_EXPERIMENTAL_OCI_REMOTE": "false",
    }


def _read_document(path: Path) -> object:
    """The JSON document Compose wrote, refused when it is larger than the limit."""
    with path.open("rb") as handle:
        data = handle.read(MAX_MODEL_BYTES + 1)
    if len(data) > MAX_MODEL_BYTES:
        raise ComposeScanError(OUTPUT_MESSAGE)
    return json.loads(data)


async def read_compose_model(
    cli: DockerCli,
    allowed: AllowedPaths,
    root: Path,
    project_directory: Path,
    files: Sequence[Path],
) -> ComposeModel:
    """Run ``docker compose config`` on the real paths of the Compose files and keep its structure.

    ``root``, ``project_directory`` and ``files`` are real paths the caller already checked
    against the allowed paths (freshly loaded in ``allowed``); ``files`` start with the base
    file. Every file the Compose files include or extend must lie inside the root and the
    allowed paths and be local, or the command is not started. Raises ``ComposeScanError`` with a
    fixed message.
    """
    executable = cli.executable
    if executable is None:
        raise ComposeScanError(CLI_MISSING_REASON)
    try:
        await asyncio.to_thread(
            check_compose_references,
            allowed,
            root,
            project_directory,
            files,
            max_bytes=MAX_COMPOSE_FILE_BYTES,
        )
    except ComposeReferenceError as error:
        raise ComposeScanError(YAML_MESSAGE if error.not_yaml else str(error)) from error
    work = Path(tempfile.mkdtemp(prefix="scan-", dir=cli.scratch))
    target = work / "config.json"
    arguments = [
        executable,
        "compose",
        "--project-directory",
        str(project_directory),
        "-p",
        PROJECT_NAME,
        "--profile",
        "*",
        *[part for path in files for part in ("-f", str(path))],
        "config",
        "--format",
        "json",
        "--no-env-resolution",
        "-o",
        str(target),
    ]
    output = BoundedOutput()
    try:
        try:
            result = await run_process(
                arguments,
                cwd=project_directory,
                env=_child_environment(cli),
                timeout=SCAN_TIMEOUT_SECONDS,
                output=output,
            )
        except OSError as error:
            raise ComposeScanError(NOT_STARTED_MESSAGE) from error
        if result.timed_out:
            raise ComposeScanError(TIMED_OUT_MESSAGE)
        if result.exit_code != 0:
            log.warning("docker compose config exited with code %s", result.exit_code)
            raise ComposeScanError(failure_message(output.text()))
        try:
            document = await asyncio.to_thread(_read_document, target)
        except (OSError, ValueError) as error:
            raise ComposeScanError(OUTPUT_MESSAGE) from error
        return extract_model(document)
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
