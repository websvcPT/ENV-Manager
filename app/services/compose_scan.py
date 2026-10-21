"""Scanning a deployment's Compose files for the services and files it could import.

``find_compose_files`` lists the Compose files that sit in the deployment root and one folder
below it. ``scan_compose`` runs the real Compose on the chosen files (``compose_config``) and maps
the structure to candidates: one service candidate per Compose service and one file candidate per
file that a service reads, in this order of appearance (services come in alphabetical order, the
way Compose prints them):

- every ``env_file`` entry, in the string, list and long forms (a long entry may be optional);
- the single-file bind mounts of ``volumes``; a folder, a named volume, a socket, a path that does
  not exist (Docker would create a folder there) and a path outside the deployment folder are
  ignored and reported in ``ignored``;
- the files of the top-level ``secrets`` and ``configs`` entries a service uses;
- the ``.env`` file of the project directory, which belongs to no service until one is chosen.

A file used by several services is one candidate with all its users and the first one as its
default owner. Paths are stored relative to the deployment root. A file outside the root or the
allowed paths is a candidate with a ``disabled_reason``, and nothing outside them is probed. No
file content is kept: a candidate holds names, sizes and attributes only.
"""

import asyncio
import grp
import os
import pwd
import re
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import get_logger
from app.models import Deployment, FileType
from app.services.compose_config import (
    MAX_COMPOSE_FILE_BYTES,
    ComposeModel,
    ComposeScanError,
    read_compose_model,
)
from app.services.confined_read import (
    ConfinedReadError,
    PathProblem,
    resolve_below_root,
    split_relative,
)
from app.services.deployments import ACCOUNT_PATTERN, MAX_ACCOUNT_LENGTH
from app.services.docker_cli import DockerCli
from app.services.environments import (
    ENVIRONMENT_NAME_PATTERN,
    MAX_ENVIRONMENT_NAME_LENGTH,
    imported_text,
)
from app.services.file_types import CERT_NAME_SUFFIXES, content_problem_for
from app.services.paths import AllowedPaths, PathValidationError, load_allowed_paths
from app.services.slug_names import MAX_SLUG_LENGTH, is_slug_name
from app.services.target_files import parse_mode

log = get_logger("compose_scan")

BASE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDE_NAMES = (
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
)
DIST_SUFFIXES = ("-dist", ".example", ".dist")
MAX_COMPOSE_FILES = 8
MAX_FOLDERS = 200
MAX_SERVICES = 200
MAX_FILES = 500

OUTSIDE_REASON = "Outside the deployment folder or the allowed paths."
FOLDER_REASON = "A folder, not a file."
SPECIAL_REASON = "Not a regular file (a socket or a device)."
MISSING_BIND_REASON = "Does not exist, so it is not known whether it is a file or a folder."
DEPLOYMENT_ENV_REASON = "The deployment environment file is not managed yet."
UNREADABLE_REASON = "The file cannot be read."
NO_NAME_REASON = "The name cannot be used as a service name."
CERT_EMPTY_REASON = "A certificate cannot be created empty."
FILE_NAME_REASON = (
    "The file name cannot be used: use letters, numbers, dots, dashes and underscores."
)

_PATH_MESSAGES = {
    PathProblem.NOT_RELATIVE: "A Compose file path must be relative to the deployment folder.",
    PathProblem.ROOT_OUTSIDE: "The deployment is outside the allowed paths.",
    PathProblem.LEAVES_ROOT: "A Compose file must be inside the deployment folder.",
}
_STATE_REASONS = {
    "outside": OUTSIDE_REASON,
    "folder": FOLDER_REASON,
    "special": SPECIAL_REASON,
    "unreadable": UNREADABLE_REASON,
    "missing": MISSING_BIND_REASON,
}
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


class FileSource(StrEnum):
    """How a Compose service reaches a file."""

    ENV_FILE = "env_file"
    BIND_MOUNT = "bind_mount"
    SECRET_FILE = "secret_file"  # noqa: S105
    CONFIG_FILE = "config_file"


class FileState(StrEnum):
    """What a path holds on the host."""

    FILE = "file"
    MISSING = "missing"
    FOLDER = "folder"
    SPECIAL = "special"
    OUTSIDE = "outside"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ComposeFileOffer:
    """A Compose file found for the user to tick: its path below the root and its kind.

    ``folder`` is the folder below the root, empty for the root itself, and ``override`` marks
    the ``*.override.*`` names that belong after a base file of the same folder.
    """

    path: str
    folder: str
    override: bool


@dataclass(frozen=True)
class ServiceCandidate:
    """A Compose service as a service of the deployment.

    ``name`` is the slug the service gets, ``compose_name`` the name in the Compose files.
    ``disabled_reason`` is set when it cannot be created. ``files`` are the keys of the file
    candidates it uses.
    """

    name: str
    compose_name: str
    profiles: tuple[str, ...]
    files: tuple[str, ...]
    disabled_reason: str | None = None


@dataclass(frozen=True)
class FileCandidate:
    """A file a service reads, as an environment of the deployment.

    ``key`` is the target path relative to the root (for a file outside the root, the path as
    Compose printed it) and identifies the candidate. ``users`` are the names of every service
    that uses it and ``owner`` the one it would belong to, None when none of them can be
    created. ``type_problems`` maps a file type value to why the file cannot have that type;
    ``file_mode``, ``file_owner`` and ``file_group`` are what the file has on the host, None
    where the deployment default applies. ``dist_suggestion`` is an existing sibling that looks
    like a template for it, looked for whether the file exists or not. ``guessed`` marks a bind
    mount whose source does not exist but whose name looks like a file's.
    """

    key: str
    name: str
    sources: tuple[FileSource, ...]
    users: tuple[str, ...]
    owner: str | None
    exists: bool
    optional: bool
    size: int | None
    file_type: FileType
    type_problems: dict[str, str]
    file_mode: str | None
    file_owner: str | None
    file_group: str | None
    dist_suggestion: str | None
    disabled_reason: str | None = None
    guessed: bool = False

    @property
    def starts_from_dist(self) -> bool:
        """Whether the file starts from its dist file by default: it is not on disk and a dist
        file was found for it."""
        return not self.exists and self.dist_suggestion is not None

    @property
    def preselected(self) -> bool:
        """Whether the file is ticked at first: usable, owned, and either on disk, starting from
        its dist file, or a required file that is not a guess."""
        return (
            self.disabled_reason is None
            and self.owner is not None
            and (self.exists or self.starts_from_dist or not (self.optional or self.guessed))
            and self.file_type.value not in self.type_problems
        )


@dataclass(frozen=True)
class IgnoredItem:
    """Something a service mounts that is not offered, with the reason."""

    label: str
    reason: str


@dataclass(frozen=True)
class ComposeScan:
    """The result of a scan: the Compose files read (relative to the root, base first), the
    service and file candidates and the items that were ignored."""

    compose_files: tuple[str, ...]
    services: tuple[ServiceCandidate, ...]
    files: tuple[FileCandidate, ...]
    ignored: tuple[IgnoredItem, ...]


@dataclass(frozen=True)
class FileDefaults:
    """The deployment's file attributes, which a file keeps when it has the same."""

    mode: int
    owner_id: int
    group_id: int


@dataclass(frozen=True)
class ResolvedFiles:
    """The chosen Compose files as real paths, with the project directory (the base file's
    folder) and the paths relative to the root."""

    project_directory: Path
    paths: tuple[Path, ...]
    relative: tuple[str, ...]


def _root_of(allowed: AllowedPaths, root_path: str) -> Path:
    """The real deployment root, or the error to show when it is outside the allowed paths."""
    try:
        return allowed.resolve(root_path)
    except PathValidationError as error:
        raise ComposeScanError("The deployment is outside the allowed paths.") from error


def _is_plain_file(path: Path) -> bool:
    """Whether the path is a regular file that is not a symbolic link."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _offers_in(folder: Path, label: str) -> list[ComposeFileOffer]:
    """The Compose files of one folder, base files before overrides."""
    offers: list[ComposeFileOffer] = []
    for names, override in ((BASE_NAMES, False), (OVERRIDE_NAMES, True)):
        for name in names:
            if _is_plain_file(folder / name):
                path = f"{label}/{name}" if label else name
                offers.append(ComposeFileOffer(path, label, override))
    return offers


def discover_compose_files(allowed: AllowedPaths, root_path: str) -> tuple[ComposeFileOffer, ...]:
    """The Compose files in the deployment root and in the folders directly below it.

    Hidden folders and symbolic links are not entered. Raises ``ComposeScanError`` when the root
    is outside the allowed paths.
    """
    root = _root_of(allowed, root_path)
    offers = _offers_in(root, "")
    try:
        with os.scandir(root) as entries:
            folders = sorted(
                entry.name
                for entry in entries
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)
            )
    except OSError as error:
        raise ComposeScanError("The deployment folder cannot be read.") from error
    for name in folders[:MAX_FOLDERS]:
        offers.extend(_offers_in(root / name, name))
    return tuple(offers)


async def find_compose_files(
    db: AsyncSession, deployment: Deployment
) -> tuple[ComposeFileOffer, ...]:
    """The Compose files offered for a deployment, read now with the allowed paths loaded fresh."""
    allowed = await load_allowed_paths(db)
    return await asyncio.to_thread(discover_compose_files, allowed, deployment.root_path)


def resolve_compose_files(
    allowed: AllowedPaths, root_path: str, raw_paths: Sequence[str]
) -> ResolvedFiles:
    """Check the chosen or typed Compose file paths and resolve them.

    Every path is relative to the root, resolved to its real location now, inside the root and
    the allowed paths, and a regular file no larger than ``MAX_COMPOSE_FILE_BYTES``. The first is
    the base file. Raises ``ComposeScanError`` with a message safe to show.
    """
    if not raw_paths:
        raise ComposeScanError("Choose at least one Compose file.")
    if len(raw_paths) > MAX_COMPOSE_FILES:
        raise ComposeScanError(f"Choose at most {MAX_COMPOSE_FILES} Compose files.")
    root = _root_of(allowed, root_path)
    real_paths: list[Path] = []
    relatives: list[str] = []
    for raw in raw_paths:
        try:
            relative = split_relative(raw, "Compose file")
            _, real = resolve_below_root(allowed, str(root), relative, "Compose file")
            info = real.stat()
        except ConfinedReadError as error:
            raise ComposeScanError(_PATH_MESSAGES.get(error.problem, UNREADABLE_REASON)) from error
        except OSError as error:
            raise ComposeScanError(f"The Compose file {raw.strip()} does not exist.") from error
        if not stat.S_ISREG(info.st_mode):
            raise ComposeScanError(f"The Compose file {raw.strip()} is not a regular file.")
        if info.st_size > MAX_COMPOSE_FILE_BYTES:
            raise ComposeScanError(f"The Compose file {raw.strip()} is too large.")
        text = relative.as_posix()
        if text in relatives:
            raise ComposeScanError(f"The Compose file {text} is chosen twice.")
        real_paths.append(real)
        relatives.append(text)
    return ResolvedFiles(real_paths[0].parent, tuple(real_paths), tuple(relatives))


def clean_service_name(raw: str) -> str | None:
    """The service name a Compose service name becomes, None when nothing usable is left.

    Letters are lowercased and every run of other characters (underscores and dots included)
    becomes one dash, which the slug rule accepts.
    """
    name = _NOT_SLUG.sub("-", raw.lower()).strip("-")[:MAX_SLUG_LENGTH].rstrip("-")
    return name if name and is_slug_name(name) else None


def assign_names(entries: Iterable[tuple[str, str, str | None]]) -> dict[str, str | None]:
    """The environment name of each file by key, from ``(key, file name, owner)`` entries.

    A name is the file name. When two files of one owner share it, the later one is prefixed with
    the name of its folder; a name that is still taken or that the environment name rule refuses
    gets None.
    """
    taken: set[tuple[str | None, str]] = set()
    names: dict[str, str | None] = {}
    for key, base, owner in entries:
        parent = PurePosixPath(key).parent.name
        candidates = [base] + ([f"{parent}-{base}"] if parent else [])
        names[key] = None
        for name in candidates:
            usable = (
                len(name) <= MAX_ENVIRONMENT_NAME_LENGTH
                and ENVIRONMENT_NAME_PATTERN.fullmatch(name) is not None
            )
            if usable and (owner, name) not in taken:
                taken.add((owner, name))
                names[key] = name
                break
    return names


@dataclass
class _Reference:
    """Everything the services say about one path, merged."""

    absolute: str
    sources: list[FileSource] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    mount_targets: list[str] = field(default_factory=list)
    optional: bool = True

    def add(
        self, source: FileSource, user: str, *, optional: bool = False, mount_target: str = ""
    ) -> None:
        """Record one use of the path, with the container path of a bind mount."""
        if source not in self.sources:
            self.sources.append(source)
        if user not in self.users:
            self.users.append(user)
        if mount_target and mount_target not in self.mount_targets:
            self.mount_targets.append(mount_target)
        self.optional = self.optional and optional


@dataclass(frozen=True)
class _Facts:
    """What the host holds at a referenced path. The attributes are those of a regular file."""

    state: FileState
    relative: str | None = None
    problem: str | None = None
    size: int | None = None
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    type_problems: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Draft:
    """A reference with what the host holds there and the key of its candidate."""

    reference: _Reference
    facts: _Facts
    key: str
    guessed: bool = False


def _references(model: ComposeModel) -> dict[str, _Reference]:
    """The paths the services reference, in order of appearance, with their sources and users.

    The deployment environment file (the ``.env`` Compose reads for interpolation) is not one of
    them unless a service names it itself.
    """
    found: dict[str, _Reference] = {}

    def use(
        path: str, source: FileSource, user: str, *, optional: bool = False, target: str = ""
    ) -> None:
        normal = os.path.normpath(path)
        found.setdefault(normal, _Reference(normal)).add(
            source, user, optional=optional, mount_target=target
        )

    for service in model.services:
        for env_file in service.env_files:
            use(env_file.path, FileSource.ENV_FILE, service.name, optional=not env_file.required)
        for bind in service.binds:
            use(bind.source, FileSource.BIND_MOUNT, service.name, target=bind.target)
        for name in service.secrets:
            if name in model.secret_files:
                use(model.secret_files[name], FileSource.SECRET_FILE, service.name)
        for name in service.configs:
            if name in model.config_files:
                use(model.config_files[name], FileSource.CONFIG_FILE, service.name)
    return found


def looks_like_file(name: str) -> bool:
    """Whether a path's last part reads like a file name: an ``.env`` name or a name with an
    extension (``nginx.conf``, ``tls.pem``). A hidden name with no other dot does not."""
    return name.startswith(".env") or bool(PurePosixPath(name).suffix)


def _inspect(allowed: AllowedPaths, root: Path, absolute: str) -> _Facts:
    """What the host holds at an absolute path, probing only inside the root and allowed paths."""
    path = PurePosixPath(absolute)
    try:
        relative = path.relative_to(PurePosixPath(root))
        if relative == PurePosixPath("."):
            return _Facts(FileState.FOLDER)
        _, real = resolve_below_root(allowed, str(root), relative, "File")
    except ValueError, ConfinedReadError:
        return _Facts(FileState.OUTSIDE)
    try:
        info = real.stat()
    except FileNotFoundError, NotADirectoryError:
        return _Facts(FileState.MISSING, relative.as_posix())
    except OSError:
        return _Facts(FileState.UNREADABLE, relative.as_posix())
    if stat.S_ISDIR(info.st_mode):
        return _Facts(FileState.FOLDER, relative.as_posix())
    if not stat.S_ISREG(info.st_mode):
        return _Facts(FileState.SPECIAL, relative.as_posix())
    text, problem = imported_text(allowed, str(root), relative.as_posix(), "target", "import")
    if problem:
        return _Facts(FileState.FILE, relative.as_posix(), problem=problem, size=info.st_size)
    type_problems = {
        file_type.value: found
        for file_type in FileType
        if (found := content_problem_for(file_type, text))
    }
    return _Facts(
        FileState.FILE,
        relative.as_posix(),
        size=info.st_size,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
        type_problems=type_problems,
    )


def default_file_type(file_name: str) -> FileType:
    """Certificate for the names that end like one, else an ENV file."""
    return FileType.CERT if file_name.lower().endswith(CERT_NAME_SUFFIXES) else FileType.ENV


def file_defaults(deployment: Deployment) -> FileDefaults:
    """The mode, owner id and group id the deployment's files get, as numbers.

    Without a configured owner or group a file keeps the user and group of the app process. A
    configured name that this host does not know matches no file.
    """
    owner_id = os.geteuid()
    group_id = os.getegid()
    if deployment.file_owner:
        owner_id = _account_id(deployment.file_owner, lambda name: pwd.getpwnam(name).pw_uid)
    if deployment.file_group:
        group_id = _account_id(deployment.file_group, lambda name: grp.getgrnam(name).gr_gid)
    return FileDefaults(parse_mode(deployment.file_mode), owner_id, group_id)


def _account_id(value: str, lookup: Callable[[str], int]) -> int:
    """The numeric id of a configured account, -1 when it does not exist here."""
    if value.isascii() and value.isdigit():
        return int(value)
    try:
        return lookup(value)
    except KeyError:
        return -1


def _account_name(numeric: int, lookup: Callable[[int], str]) -> str:
    """The name of an account when it has a usable one, else its number."""
    try:
        name = lookup(numeric)
    except KeyError:
        return str(numeric)
    if len(name) <= MAX_ACCOUNT_LENGTH and ACCOUNT_PATTERN.fullmatch(name):
        return name
    return str(numeric)


def file_attributes(
    mode: int | None, uid: int | None, gid: int | None, defaults: FileDefaults
) -> tuple[str | None, str | None, str | None]:
    """The mode, owner and group of a file on the host as stored, None for each that is the
    deployment default (or unknown, for a file that does not exist). An owner or group is a name
    when this host has a usable one for the id, else the number."""
    if mode is None or uid is None or gid is None:
        return None, None, None
    stored_mode = None if mode == defaults.mode else f"{mode:04o}"
    owner = (
        None
        if uid == defaults.owner_id
        else _account_name(uid, lambda number: pwd.getpwuid(number).pw_name)
    )
    group = (
        None
        if gid == defaults.group_id
        else _account_name(gid, lambda number: grp.getgrgid(number).gr_name)
    )
    return stored_mode, owner, group


def _dist_suggestion(allowed: AllowedPaths, root: Path, relative: str) -> str | None:
    """The first existing sibling of the file named ``<name>-dist``, ``<name>.example`` or
    ``<name>.dist``."""
    path = PurePosixPath(relative)
    for suffix in DIST_SUFFIXES:
        sibling = path.with_name(path.name + suffix)
        try:
            _, real = resolve_below_root(allowed, str(root), sibling, "Dist")
        except ConfinedReadError:
            continue
        if _is_plain_file(real):
            return sibling.as_posix()
    return None


def _service_candidates(
    model: ComposeModel, files_of: dict[str, list[str]]
) -> list[ServiceCandidate]:
    """The service candidates: names cleaned to the slug rule, a name taken twice refused."""
    candidates: list[ServiceCandidate] = []
    used: set[str] = set()
    for service in model.services:
        name = clean_service_name(service.name)
        reason: str | None = None
        if name is None:
            reason = NO_NAME_REASON
            name = service.name
        elif name in used:
            reason = f"Another service is already named {name}."
        else:
            used.add(name)
        candidates.append(
            ServiceCandidate(
                name=name,
                compose_name=service.name,
                profiles=service.profiles,
                files=tuple(files_of.get(service.name, ())),
                disabled_reason=reason,
            )
        )
    return candidates


def _disabled_reason(facts: _Facts) -> str | None:
    """Why a file candidate cannot be imported. A missing file is fine: it is created empty."""
    if facts.state is FileState.FILE:
        return facts.problem
    if facts.state is FileState.MISSING:
        return None
    return _STATE_REASONS[facts.state]


def _file_candidate(
    allowed: AllowedPaths,
    root: Path,
    draft: _Draft,
    name: str | None,
    owner: str | None,
    users: tuple[str, ...],
    defaults: FileDefaults,
) -> FileCandidate:
    """The candidate of one referenced file, with the type, attributes and dist suggestion."""
    facts = draft.facts
    file_name = PurePosixPath(draft.key).name
    file_type = default_file_type(file_name)
    type_problems = dict(facts.type_problems)
    if facts.state is FileState.MISSING and file_type is FileType.CERT:
        type_problems[FileType.CERT.value] = CERT_EMPTY_REASON
    reason = _disabled_reason(facts)
    if reason is None and name is None:
        reason = FILE_NAME_REASON
    mode, file_owner, file_group = file_attributes(facts.mode, facts.uid, facts.gid, defaults)
    suggestion = None
    if file_type is FileType.ENV and facts.state in (FileState.FILE, FileState.MISSING):
        suggestion = _dist_suggestion(allowed, root, draft.key)
    return FileCandidate(
        key=draft.key,
        name=name or file_name,
        sources=tuple(draft.reference.sources),
        users=users,
        owner=owner,
        exists=facts.state is FileState.FILE,
        optional=draft.reference.optional,
        size=facts.size,
        file_type=file_type,
        type_problems=type_problems,
        file_mode=mode,
        file_owner=file_owner,
        file_group=file_group,
        dist_suggestion=suggestion,
        disabled_reason=reason,
        guessed=draft.guessed,
    )


def _missing_mount_is_file(allowed: AllowedPaths, root: Path, key: str, targets: list[str]) -> bool:
    """Whether a bind mount source that does not exist is taken for a file: a dist file sits
    beside it, or its own name or a container path it is mounted at reads like a file name."""
    if _dist_suggestion(allowed, root, key) is not None:
        return True
    names = [PurePosixPath(key).name, *(PurePosixPath(target).name for target in targets)]
    return any(looks_like_file(name) for name in names if name)


def _drafts(
    allowed: AllowedPaths, root: Path, references: Iterable[_Reference]
) -> tuple[list[_Draft], list[IgnoredItem]]:
    """Inspect every reference. A bind mount that is not a regular file is ignored, unless it
    does not exist and looks like a file; every other reference stays as a candidate."""
    drafts: list[_Draft] = []
    ignored: list[IgnoredItem] = []
    for reference in references:
        facts = _inspect(allowed, root, reference.absolute)
        key = facts.relative if facts.relative is not None else reference.absolute
        if set(reference.sources) == {FileSource.BIND_MOUNT} and facts.state is not FileState.FILE:
            if facts.state is FileState.MISSING and _missing_mount_is_file(
                allowed, root, key, reference.mount_targets
            ):
                drafts.append(_Draft(reference, facts, key, guessed=True))
                continue
            ignored.append(IgnoredItem(key, _STATE_REASONS[facts.state]))
            continue
        drafts.append(_Draft(reference, facts, key))
    return drafts, ignored


def _deployment_env(
    allowed: AllowedPaths, root: Path, project_directory: Path, references: dict[str, _Reference]
) -> IgnoredItem | None:
    """The deployment environment file beside the base Compose file as an item not offered, when
    it exists and no service names it itself."""
    path = project_directory / ".env"
    if os.path.normpath(str(path)) in references:
        return None
    facts = _inspect(allowed, root, str(path))
    if facts.state is not FileState.FILE or facts.relative is None:
        return None
    return IgnoredItem(facts.relative, DEPLOYMENT_ENV_REASON)


def build_scan(
    allowed: AllowedPaths,
    root: Path,
    resolved: ResolvedFiles,
    model: ComposeModel,
    defaults: FileDefaults,
) -> ComposeScan:
    """Map the Compose structure to candidates, reading the host files of every reference.

    Blocking: it stats and reads files, so callers run it in a thread.
    """
    if len(model.services) > MAX_SERVICES:
        raise ComposeScanError(f"The Compose files define more than {MAX_SERVICES} services.")
    references = _references(model)
    if len(references) > MAX_FILES:
        raise ComposeScanError(f"The Compose files name more than {MAX_FILES} files.")
    by_compose_name = {service.compose_name: service for service in _service_candidates(model, {})}
    drafts, ignored = _drafts(allowed, root, references.values())
    deployment_env = _deployment_env(allowed, root, resolved.project_directory, references)
    if deployment_env is not None:
        ignored.append(deployment_env)
    owners: dict[str, str | None] = {}
    for draft in drafts:
        enabled = [
            by_compose_name[user].name
            for user in draft.reference.users
            if by_compose_name[user].disabled_reason is None
        ]
        owners[draft.key] = enabled[0] if enabled else None
    names = assign_names(
        (draft.key, PurePosixPath(draft.key).name, owners[draft.key]) for draft in drafts
    )
    files: list[FileCandidate] = []
    keys_of: dict[str, list[str]] = {}
    for draft in drafts:
        users = tuple(by_compose_name[user].name for user in draft.reference.users)
        files.append(
            _file_candidate(
                allowed, root, draft, names[draft.key], owners[draft.key], users, defaults
            )
        )
        for user in draft.reference.users:
            keys_of.setdefault(user, []).append(draft.key)
    services = tuple(_service_candidates(model, keys_of))
    return ComposeScan(resolved.relative, services, tuple(files), tuple(ignored))


async def scan_compose(
    db: AsyncSession, cli: DockerCli, deployment: Deployment, compose_files: Sequence[str]
) -> ComposeScan:
    """Scan the chosen Compose files of a deployment for services and files to import.

    The allowed paths are loaded fresh, the files are resolved and checked, the real Compose
    reads them and the host files they name are inspected. ``compose_files`` are paths relative
    to the root, the base file first. Raises ``ComposeScanError`` with a message safe to show.
    """
    allowed = await load_allowed_paths(db)
    resolved = await asyncio.to_thread(
        resolve_compose_files, allowed, deployment.root_path, compose_files
    )
    root = _root_of(allowed, deployment.root_path)
    model = await read_compose_model(cli, allowed, root, resolved.project_directory, resolved.paths)
    defaults = file_defaults(deployment)
    return await asyncio.to_thread(build_scan, allowed, root, resolved, model, defaults)
