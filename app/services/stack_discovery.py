"""Finding the Compose stacks on this host that a deployment could be made for.

Two sources are merged by the real folder of the stack:

- Docker, when the daemon answers: ``docker compose ls --all`` names every project with its
  status and the Compose files it was started from. The project folder is the
  ``com.docker.compose.project.working_dir`` label of its containers, read with a template that
  selects that label and the project label only (never ``Config.Env``), and otherwise the folder
  of the first Compose file. The files are the ones Docker reports, in its order, and count only
  when every one of them exists inside that folder.
- The allowed paths: every usable allowed root is walked ``MAX_WALK_DEPTH`` levels down (hidden
  folders and symbolic links are not entered, at most ``MAX_FOLDERS`` folders per root) and a
  folder with a base Compose file is a stack, with the files Compose loads by default there.

Every path goes through the allowed paths. A stack outside them is kept but not usable, with a
fixed reason, because production mounts host folders at the same path inside the container and a
path the app may not use is still worth showing. A stack is marked as used when a deployment has
the same real root or the same Compose project name. When Docker is not usable the folder source
stands alone and the result says why Docker was not used.

Nothing here changes anything on the host, and no message from Docker is passed on: failures
become the fixed sentences of ``docker_query``.
"""

import asyncio
import json
import os
import re
from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deployment
from app.services.compose_project import is_compose_project_name
from app.services.compose_scan import MAX_COMPOSE_FILES, MAX_FOLDERS, default_compose_files
from app.services.docker_cli import DockerCli
from app.services.docker_query import DockerQueryError, run_docker_query
from app.services.paths import AllowedPaths, PathValidationError, is_within, normalize_path

MAX_WALK_DEPTH = 2
MAX_DOCKER_PROJECTS = 500
PROJECT_LABEL = "com.docker.compose.project"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
LABEL_FORMAT = (
    f'{{"project":{{{{json (.Label "{PROJECT_LABEL}")}}}},'
    f'"folder":{{{{json (.Label "{WORKING_DIR_LABEL}")}}}}}}'
)

OUTSIDE_REASON = "The folder is outside the allowed paths."
NO_FILES_REASON = "The Compose files were not found inside the folder."
TOO_MANY_FILES_REASON = f"The stack uses more than {MAX_COMPOSE_FILES} Compose files."
UNREADABLE_REASON = "Docker gave an answer that could not be read."
NOT_USABLE_REASON = "Docker is not usable here."

_STATUS_PART = re.compile(r"([a-z]+)\((\d+)\)")


class StackSource(StrEnum):
    """Where a stack was found."""

    DOCKER = "docker"
    FOLDER = "folder"
    BOTH = "both"


@dataclass(frozen=True)
class ExistingDeployment:
    """The deployment that already uses a stack."""

    id: int
    name: str


@dataclass(frozen=True)
class DetectedStack:
    """One stack found on the host.

    ``project`` is the Compose project name, None for a folder-only find. ``folder`` is the real
    path (the path as given when it is outside the allowed paths) and ``compose_files`` are paths
    relative to it, base first. ``running`` and ``total`` count the containers when Docker knows
    the project. ``reason`` says why a stack is not ``usable``. ``used_by`` is the deployment
    that already has this root or project.
    """

    project: str | None
    folder: str
    compose_files: tuple[str, ...]
    running: int | None
    total: int | None
    source: StackSource
    usable: bool
    reason: str | None = None
    used_by: ExistingDeployment | None = None

    @property
    def name(self) -> str:
        """The name to show and sort by: the project, else the name of the folder."""
        return self.project or Path(self.folder).name or self.folder


@dataclass(frozen=True)
class StackDiscovery:
    """The stacks found, usable first, and why Docker was not used (None when it was)."""

    stacks: tuple[DetectedStack, ...]
    docker_reason: str | None = None


@dataclass(frozen=True)
class DockerProject:
    """A project of ``docker compose ls``, as read."""

    name: str
    config_files: tuple[str, ...]
    running: int | None
    total: int | None


@dataclass(frozen=True)
class _UsedBy:
    """A deployment as the stacks are compared with it: its real root and its project name."""

    deployment: ExistingDeployment
    root: str | None
    project: str | None


class _UnreadableAnswer(Exception):
    """Docker answered with something that is not the expected JSON."""


def _status_counts(status: object) -> tuple[int | None, int | None]:
    """The running and the total container count of a ``compose ls`` status such as
    ``running(2), exited(1)``; None for both when the text holds no count."""
    if not isinstance(status, str):
        return None, None
    parts = _STATUS_PART.findall(status)
    if not parts:
        return None, None
    total = sum(int(count) for _, count in parts)
    running = sum(int(count) for state, count in parts if state == "running")
    return running, total


def parse_projects(text: str) -> list[DockerProject] | None:
    """The projects of the JSON array ``compose ls`` printed, None when it is not one.

    An entry that is not an object, has no valid project name or names no Compose file is
    skipped.
    """
    try:
        document = json.loads(text)
    except ValueError:
        return None
    if not isinstance(document, list):
        return None
    projects: list[DockerProject] = []
    for entry in document[:MAX_DOCKER_PROJECTS]:
        if not isinstance(entry, dict):
            continue
        name, files = entry.get("Name"), entry.get("ConfigFiles")
        if not isinstance(name, str) or not is_compose_project_name(name):
            continue
        if not isinstance(files, str):
            continue
        config_files = tuple(part.strip() for part in files.split(",") if part.strip())
        if config_files:
            running, total = _status_counts(entry.get("Status"))
            projects.append(DockerProject(name, config_files, running, total))
    return projects


def parse_working_dirs(text: str) -> dict[str, str]:
    """The working directory label of each project, from one JSON object per line."""
    folders: dict[str, str] = {}
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        project, folder = entry.get("project"), entry.get("folder")
        if isinstance(project, str) and isinstance(folder, str) and folder:
            folders.setdefault(project, folder)
    return folders


async def _docker_projects(cli: DockerCli) -> tuple[list[DockerProject], dict[str, str]]:
    """The Compose projects Docker knows and their working directory labels.

    Raises ``DockerQueryError`` when the project list cannot be fetched and ``_UnreadableAnswer``
    when it cannot be read. A failure of the label lookup only costs the labels: the folder then
    comes from the Compose files.
    """
    listing = await run_docker_query(cli, ["compose", "ls", "--all", "--format", "json"])
    projects = parse_projects(listing)
    if projects is None:
        raise _UnreadableAnswer
    try:
        labels = await run_docker_query(
            cli, ["ps", "--all", "--filter", f"label={PROJECT_LABEL}", "--format", LABEL_FORMAT]
        )
    except DockerQueryError:
        return projects, {}
    return projects, parse_working_dirs(labels)


def _locate(allowed: AllowedPaths, raw: str) -> tuple[Path, bool] | None:
    """The folder of a path text and whether it is inside the allowed paths.

    Inside, it is the real path. Outside, it is the normalized path as given and nothing of it is
    read. None when the text is not an absolute path.
    """
    try:
        lexical = normalize_path(raw)
    except PathValidationError:
        return None
    try:
        return allowed.resolve(lexical), True
    except PathValidationError:
        return lexical, False


def _docker_files(
    allowed: AllowedPaths, folder: Path, config_files: tuple[str, ...]
) -> tuple[str, ...]:
    """The Compose files of a project relative to its folder, in Docker's order.

    Empty unless every file is inside the folder and the allowed paths and exists, because a
    part of the set would not start the same project.
    """
    relative: list[str] = []
    for raw in config_files:
        try:
            path = normalize_path(raw)
            real = allowed.resolve(path.parent) / path.name
        except PathValidationError:
            return ()
        if not is_within(real, folder) or not real.is_file() or not allowed.contains(real):
            return ()
        text = real.relative_to(folder).as_posix()
        if text not in relative:
            relative.append(text)
    return tuple(relative)


def _subfolders(folder: Path) -> list[Path]:
    """The folders directly below a folder, sorted, without hidden ones and symbolic links."""
    try:
        with os.scandir(folder) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        return []
    return [folder / name for name in names]


def _walk_roots(allowed: AllowedPaths) -> dict[Path, tuple[str, ...]]:
    """The folders with Compose files in the allowed roots and below, with those files."""
    found: dict[Path, tuple[str, ...]] = {}
    for root in allowed.usable_roots():
        queue = deque([(root, 0)])
        scanned = 0
        while queue and scanned < MAX_FOLDERS:
            folder, depth = queue.popleft()
            scanned += 1
            if files := default_compose_files(folder):
                found.setdefault(folder, files)
            if depth < MAX_WALK_DEPTH:
                queue.extend((child, depth + 1) for child in _subfolders(folder))
    return found


def _users_of(allowed: AllowedPaths, rows: list[tuple[int, str, str, str | None]]) -> list[_UsedBy]:
    """The deployments as ``_UsedBy``, with the root resolved when it is inside the allowed
    paths and as given when it is not."""
    users: list[_UsedBy] = []
    for deployment_id, name, root_path, project in rows:
        located = _locate(allowed, root_path)
        root = str(located[0]) if located is not None else None
        users.append(_UsedBy(ExistingDeployment(deployment_id, name), root, project))
    return users


def _used_by(users: list[_UsedBy], project: str | None, folder: str) -> ExistingDeployment | None:
    """The first deployment (by id) with the project name or the folder as its root."""
    for user in users:
        if (project is not None and user.project == project) or user.root == folder:
            return user.deployment
    return None


def _docker_stack(
    allowed: AllowedPaths,
    project: DockerProject,
    labels: dict[str, str],
    folder_files: dict[Path, tuple[str, ...]],
) -> DetectedStack | None:
    """The stack of a Docker project, None when no folder can be made of it."""
    located = None
    for raw in (labels.get(project.name), str(PurePosixPath(project.config_files[0]).parent)):
        located = _locate(allowed, raw) if raw else None
        if located is not None:
            break
    if located is None:
        return None
    folder, inside = located
    source = StackSource.DOCKER
    files: tuple[str, ...] = ()
    reason: str | None = OUTSIDE_REASON
    if inside:
        files = _docker_files(allowed, folder, project.config_files)
        if not files and folder in folder_files:
            files = folder_files[folder]
        if folder in folder_files:
            source = StackSource.BOTH
        reason = None
        if not files:
            reason = NO_FILES_REASON
        elif len(files) > MAX_COMPOSE_FILES:
            files, reason = files[:MAX_COMPOSE_FILES], TOO_MANY_FILES_REASON
    return DetectedStack(
        project=project.name,
        folder=str(folder),
        compose_files=files,
        running=project.running,
        total=project.total,
        source=source,
        usable=reason is None,
        reason=reason,
    )


def assemble_stacks(
    allowed: AllowedPaths,
    projects: list[DockerProject],
    labels: dict[str, str],
    rows: list[tuple[int, str, str, str | None]],
) -> tuple[DetectedStack, ...]:
    """Merge the Docker projects and the folders found in the allowed roots, mark the stacks a
    deployment already uses and sort them, usable first and then by name.

    Blocking: it walks the allowed roots and reads files, so callers run it in a thread.
    """
    folder_files = _walk_roots(allowed)
    stacks: list[DetectedStack] = []
    seen: set[str] = set()
    for project in projects:
        if (stack := _docker_stack(allowed, project, labels, folder_files)) is not None:
            stacks.append(stack)
            seen.add(stack.folder)
    for folder, files in folder_files.items():
        if str(folder) not in seen:
            stacks.append(
                DetectedStack(None, str(folder), files, None, None, StackSource.FOLDER, True)
            )
    users = _users_of(allowed, rows)
    marked = [
        replace(stack, used_by=_used_by(users, stack.project, stack.folder)) for stack in stacks
    ]
    marked.sort(key=lambda stack: (not stack.usable, stack.name.casefold(), stack.folder))
    return tuple(marked)


async def discover_stacks(
    db: AsyncSession, cli: DockerCli, allowed: AllowedPaths
) -> StackDiscovery:
    """Find the stacks of the host from Docker (when it is usable) and from the allowed paths.

    The Docker status is checked fresh because the answer decides whether Docker is asked.
    ``allowed`` is loaded by the caller at the moment of the request.
    """
    projects: list[DockerProject] = []
    labels: dict[str, str] = {}
    docker_reason: str | None = None
    status = await cli.status(fresh=True)
    if status.available:
        try:
            projects, labels = await _docker_projects(cli)
        except DockerQueryError as error:
            docker_reason = error.message
        except _UnreadableAnswer:
            docker_reason = UNREADABLE_REASON
    else:
        docker_reason = status.reason or NOT_USABLE_REASON
    result = await db.execute(
        select(
            Deployment.id, Deployment.name, Deployment.root_path, Deployment.compose_project
        ).order_by(Deployment.id)
    )
    rows = [(row.id, row.name, row.root_path, row.compose_project) for row in result]
    stacks = await asyncio.to_thread(assemble_stacks, allowed, projects, labels, rows)
    return StackDiscovery(stacks, docker_reason)
