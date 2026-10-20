"""Checking the files a set of Compose files read, before the real Compose runs.

Compose follows the ``include`` entries and the ``extends`` files of a Compose file wherever they
point, and reads the ``env_file`` of a long form include. So the Compose files are parsed with
``yaml.safe_load`` first (only those two keys are walked, nothing else is built) and every target
must be a path that lies inside the deployment root and the allowed paths once symbolic links are
followed. A remote source (git, http or OCI) and a path with a variable are refused, because the
first leaves the host and the second is expanded by Compose from the environment, so where it
points cannot be known here.

Relative paths resolve as Compose resolves them, which was checked on the installed Compose: the
files given on the command line (overrides included) against the project directory, an included
file against the folder of the file that includes it (or the ``project_directory`` of the entry),
and the nested includes and extends of that file against the same folder, and an extended file
against its own folder. The walk is bounded in depth and in the number of files and never visits
the same file twice for the same folder, so a cycle ends it.

Messages name the entry as written (shortened) and never anything else of a file. A target that
does not exist is left for Compose to report.
"""

import os
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.services.paths import AllowedPaths, PathValidationError, is_within

MAX_REFERENCED_FILES = 50
MAX_DEPTH = 8
MAX_SHOWN_LENGTH = 80

NOT_YAML_MESSAGE = "A Compose file is not valid YAML or does not follow the Compose format."
TOO_MANY_MESSAGE = (
    "The Compose files include or extend too many other files, or nest them too deeply, "
    "so nothing was scanned."
)
TOO_LARGE_MESSAGE = "A file named by an include or an extends is too large to scan."

_REMOTE_REFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|[\w.-]+@[\w.-]+:|\.git(?:[?#]|$)")
_UNSHOWN = re.compile(r"[^\x20-\x7e]")
_FIELDS = {
    "include": "an include",
    "extends": "an extends",
    "env_file": "the env_file of an include",
    "project_directory": "the project_directory of an include",
}
_OUTSIDE_ACTIONS = {
    "include": "include the file",
    "extends": "extend a service from the file",
    "env_file": "read the env file",
    "project_directory": "use the project directory",
}


class ComposeReferenceError(Exception):
    """A Compose file names something that is refused; the message is fixed text that is safe to
    show. ``not_yaml`` is set when a file could not be parsed, so the caller can use its own
    sentence for that."""

    def __init__(self, message: str, *, not_yaml: bool = False) -> None:
        """Hold the message and whether the cause was a file that is not YAML."""
        super().__init__(message)
        self.not_yaml = not_yaml


@dataclass(frozen=True)
class _Pending:
    """A file to parse, the folder its relative paths resolve against and its nesting depth."""

    path: Path
    context: Path
    depth: int


@dataclass(frozen=True)
class _Include:
    """One ``include`` entry: its files, its ``project_directory`` and its env files."""

    paths: tuple[str, ...]
    project_directory: str | None
    env_files: tuple[str, ...]


def _shown(entry: str) -> str:
    """The entry as written, with anything but printable ASCII removed and long ones cut."""
    text = _UNSHOWN.sub("?", entry)
    return text if len(text) <= MAX_SHOWN_LENGTH else f"{text[:MAX_SHOWN_LENGTH]}..."


def _strings(value: object) -> tuple[str, ...]:
    """The value as a tuple of strings: a string alone or the strings of a list."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _includes(document: dict[object, object]) -> list[_Include]:
    """The ``include`` entries of a document in the short and the long form."""
    entries = document.get("include")
    found: list[_Include] = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            found.append(_Include((entry,), None, ()))
        elif isinstance(entry, dict):
            directory = entry.get("project_directory")
            found.append(
                _Include(
                    _strings(entry.get("path")),
                    directory if isinstance(directory, str) else None,
                    _strings(entry.get("env_file")),
                )
            )
    return found


def _extends_files(document: dict[object, object]) -> list[str]:
    """The ``file`` of each service that extends a service of another file."""
    services = document.get("services")
    files: list[str] = []
    for service in services.values() if isinstance(services, dict) else []:
        extends = service.get("extends") if isinstance(service, dict) else None
        name = extends.get("file") if isinstance(extends, dict) else None
        if isinstance(name, str):
            files.append(name)
    return files


def _load(path: Path, max_bytes: int) -> dict[object, object]:
    """The mapping a Compose file holds, empty when it is missing, not a file or not a mapping.

    Raises ``ComposeReferenceError`` when the file is too large or is not YAML.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return {}
    if len(data) > max_bytes:
        raise ComposeReferenceError(TOO_LARGE_MESSAGE)
    try:
        document = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError) as error:
        raise ComposeReferenceError(NOT_YAML_MESSAGE, not_yaml=True) from error
    return document if isinstance(document, dict) else {}


def _real_of(allowed: AllowedPaths, path: Path) -> Path:
    """The real path of a file that was already checked, the path itself when it cannot be
    resolved now."""
    try:
        return allowed.resolve(path)
    except PathValidationError:
        return path


def _locate(entry: str, context: Path, kind: str, allowed: AllowedPaths, root: Path) -> Path:
    """The path an entry names, cleaned as Compose cleans it, or the error that refuses it.

    The path is refused when it is remote, holds a variable, or does not resolve (symbolic links
    followed) to a place inside the root and the allowed paths.
    """
    shown = _shown(entry)
    if _REMOTE_REFERENCE.search(entry):
        raise ComposeReferenceError(
            f'The Compose files include the remote source "{shown}". '
            "Only local files are read, so nothing was scanned."
        )
    if "$" in entry:
        raise ComposeReferenceError(
            f'The path "{shown}" in {_FIELDS[kind]} uses a variable, so where it points cannot '
            "be checked. Write the path out. Nothing was scanned."
        )
    outside = ComposeReferenceError(
        f'The Compose files {_OUTSIDE_ACTIONS[kind]} "{shown}", which is outside the deployment '
        "folder or the allowed paths. Nothing was scanned."
    )
    if "\x00" in entry:
        raise outside
    cleaned = Path(os.path.normpath(context / entry))
    try:
        real = allowed.resolve(cleaned)
    except PathValidationError as error:
        raise outside from error
    if not is_within(real, root):
        raise outside
    return cleaned


def check_compose_references(
    allowed: AllowedPaths,
    root: Path,
    project_directory: Path,
    files: Sequence[Path],
    *,
    max_bytes: int,
) -> None:
    """Refuse the Compose files when any file they include or extend lies outside the root.

    ``root`` is the real deployment root, ``project_directory`` the real folder Compose resolves
    the given ``files`` against (the base file's folder), and ``files`` the real paths of the
    chosen Compose files. ``allowed`` must be freshly loaded. Raises ``ComposeReferenceError``.
    """
    visited: set[tuple[Path, Path]] = set()
    queue = deque(_Pending(path, project_directory, 0) for path in files)
    while queue:
        pending = queue.popleft()
        real = _real_of(allowed, pending.path)
        if (real, pending.context) in visited:
            continue
        visited.add((real, pending.context))
        if len(visited) > MAX_REFERENCED_FILES + len(files):
            raise ComposeReferenceError(TOO_MANY_MESSAGE)
        document = _load(real, max_bytes)
        children: list[_Pending] = []
        for include in _includes(document):
            context: Path | None = None
            if include.project_directory is not None:
                context = _locate(
                    include.project_directory, pending.context, "project_directory", allowed, root
                )
            for env_file in include.env_files:
                _locate(env_file, pending.context, "env_file", allowed, root)
            for entry in include.paths:
                target = _locate(entry, pending.context, "include", allowed, root)
                folder = target.parent if context is None else context
                children.append(_Pending(target, folder, pending.depth + 1))
        for entry in _extends_files(document):
            target = _locate(entry, pending.context, "extends", allowed, root)
            children.append(_Pending(target, target.parent, pending.depth + 1))
        if children and pending.depth + 1 > MAX_DEPTH:
            raise ComposeReferenceError(TOO_MANY_MESSAGE)
        queue.extend(children)
