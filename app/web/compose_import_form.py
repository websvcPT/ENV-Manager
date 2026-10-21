"""What the Docker Compose import screens post, and how a fresh scan judges it.

The screens carry the user's choices in the form and never keep the scan: every step reads the
Compose files again, so what a step shows or imports is what the files hold now. The posted names
are only choices, matched against the fresh scan by key. Files are named by their key in the
fields (``owner:<key>``, ``type:<key>``, ``dist:<key>``, ``start:<key>``), not by their position,
so a scan that lists them in another order does not shift a choice onto another file. The
deployment files are ticked as ``deployment_file`` and share the per key fields. A post-deploy
command is posted per owner, a service name or ``DEPLOYMENT_FILES_NAME`` (which no service can
be named): its text as ``command:<owner>`` and its tick as ``run:<owner>``.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from starlette.datastructures import FormData

from app.models import DEPLOYMENT_FILES_NAME, FileType
from app.services.compose_import import (
    COMMAND_PREFIX,
    COMMAND_STORED_MESSAGE,
    DIST_NEEDED_MESSAGE,
    EMPTY_COMMAND_MESSAGE,
    NO_SERVICE_MESSAGE,
    ComposeSelection,
    FileChoice,
)
from app.services.compose_scan import ComposeScan, FileCandidate
from app.services.hook_commands import Command, CommandError, parse_commands
from app.services.hook_settings import UNAVAILABLE_SUFFIX

OWNER_PREFIX = "owner:"
TYPE_PREFIX = "type:"
DIST_PREFIX = "dist:"
START_PREFIX = "start:"
RUN_PREFIX = "run:"
GONE_MESSAGE = "This file is no longer found in the Compose files."
SERVICES_LABEL = "Services"
COMMANDS_LABEL = "Post-deploy commands"
COMMAND_FILES_MESSAGE = (
    "Select a deployment file for the post-deploy command of the deployment files, or untick it."
)


@dataclass(frozen=True)
class PostedImport:
    """The fields of an import step: the Compose files, the ticked services, the ticked files and
    deployment files by key, the owner, type, dist path and start-from-dist choice posted for
    each key, and the command text and tick posted for each owner."""

    compose_files: tuple[str, ...]
    services: tuple[str, ...]
    picked: tuple[str, ...]
    owners: dict[str, str]
    types: dict[str, str]
    dists: dict[str, str]
    starts: dict[str, str]
    deployment_picked: tuple[str, ...] = ()
    commands: dict[str, str] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FileRow:
    """One file of the preview: its candidate, the services that may own it, the owner, type and
    dist choices it starts with, and whether it starts ticked. ``linked`` ticks the dist link of
    every dist file found, the file on disk or not; ``from_dist`` ticks starting from the dist
    file, for a file that is not on disk."""

    candidate: FileCandidate
    owner_options: tuple[str, ...]
    owner: str
    file_type: str
    checked: bool
    from_dist: bool
    linked: bool = False


@dataclass(frozen=True)
class Review:
    """A posted choice judged against a fresh scan.

    ``selection`` is what the import takes, built only from the scan; ``picks`` are the files in
    it with their candidates (the deployment files last); ``dropped`` are ticked files left out
    because none of the services that use them is ticked; ``commands`` are the ticked commands
    with the words each line is run with; ``problems`` are (label, message) pairs, empty when it
    can go on.
    """

    selection: ComposeSelection
    picks: tuple[tuple[FileCandidate, FileChoice], ...]
    dropped: tuple[str, ...]
    problems: tuple[tuple[str, str], ...]
    commands: tuple[tuple[str, tuple[Command, ...]], ...] = ()


def _texts(form: FormData, name: str) -> tuple[str, ...]:
    """The distinct, non-empty text values posted under a name, in order."""
    values = (value.strip() for value in form.getlist(name) if isinstance(value, str))
    return tuple(dict.fromkeys(value for value in values if value))


def _prefixed(form: FormData, prefix: str) -> dict[str, str]:
    """The text fields whose name starts with the prefix, keyed by the rest of the name."""
    return {
        name[len(prefix) :]: value.strip()
        for name, value in form.multi_items()
        if name.startswith(prefix) and isinstance(value, str)
    }


def parse_posted(form: FormData) -> PostedImport:
    """Read the fields of an import step. Nothing is checked here."""
    return PostedImport(
        compose_files=_texts(form, "compose_file"),
        services=_texts(form, "service"),
        picked=_texts(form, "file"),
        owners=_prefixed(form, OWNER_PREFIX),
        types=_prefixed(form, TYPE_PREFIX),
        dists=_prefixed(form, DIST_PREFIX),
        starts=_prefixed(form, START_PREFIX),
        deployment_picked=_texts(form, "deployment_file"),
        commands=_prefixed(form, COMMAND_PREFIX),
        runs=_prefixed(form, RUN_PREFIX),
    )


def owner_options(candidate: FileCandidate, usable: Sequence[str]) -> tuple[str, ...]:
    """The services that may own a file: the usable ones that use it."""
    return tuple(dict.fromkeys(name for name in candidate.users if name in usable))


def usable_services(scan: ComposeScan) -> tuple[str, ...]:
    """The names of the services that can be created, in scan order."""
    return tuple(service.name for service in scan.services if service.disabled_reason is None)


def preview_rows(scan: ComposeScan) -> tuple[FileRow, ...]:
    """The file rows of a first preview, each with its default choices."""
    usable = usable_services(scan)
    return tuple(
        FileRow(
            candidate=candidate,
            owner_options=owner_options(candidate, usable),
            owner=candidate.owner or "",
            file_type=candidate.file_type.value,
            checked=candidate.preselected,
            from_dist=candidate.starts_from_dist,
            linked=candidate.dist_suggestion is not None,
        )
        for candidate in scan.files
    )


def deployment_rows(scan: ComposeScan) -> tuple[FileRow, ...]:
    """The rows of the deployment files in a first preview, each with its default choices."""
    return tuple(
        FileRow(
            candidate=candidate,
            owner_options=(DEPLOYMENT_FILES_NAME,),
            owner=DEPLOYMENT_FILES_NAME,
            file_type=candidate.file_type.value,
            checked=candidate.preselected,
            from_dist=candidate.starts_from_dist,
            linked=candidate.dist_suggestion is not None,
        )
        for candidate in scan.deployment_files
    )


def _file_type(text: str, candidate: FileCandidate) -> FileType:
    """The posted type, the candidate's own when the text is not a file type."""
    try:
        return FileType(text)
    except ValueError:
        return candidate.file_type


def _judge(candidate: FileCandidate, owner: str, posted: PostedImport) -> FileChoice | str:
    """The choice of a ticked file with a known owner, or why it cannot be imported. The dist
    file is the fresh scan's suggestion, taken only for an ENV file; a file that is not on disk
    starts from it only when it is linked."""
    key = candidate.key
    file_type = _file_type(posted.types.get(key, ""), candidate)
    if file_type.value in candidate.type_problems:
        return candidate.type_problems[file_type.value]
    wants_dist = bool(posted.dists.get(key)) and file_type is FileType.ENV
    dist_path = candidate.dist_suggestion if wants_dist else None
    from_dist = bool(posted.starts.get(key)) and not candidate.exists
    if from_dist and dist_path is None:
        return DIST_NEEDED_MESSAGE
    return FileChoice(key, owner, file_type, dist_path, from_dist)


def _review_commands(
    posted: PostedImport, owners: Sequence[str], docker_reason: str | None
) -> tuple[list[tuple[str, tuple[Command, ...]]], list[tuple[str, str]]]:
    """The ticked commands of the given owners with their parsed lines, and the problems.

    A ticked command of an owner that is not chosen is left out, except the one of the deployment
    files, which is a problem when none of their files is ticked. ``docker_reason`` is why
    commands cannot be stored now, None when they can.
    """
    commands: list[tuple[str, tuple[Command, ...]]] = []
    problems: list[tuple[str, str]] = []
    ticked = [owner for owner in posted.runs if posted.runs[owner]]
    if DEPLOYMENT_FILES_NAME in ticked and DEPLOYMENT_FILES_NAME not in owners:
        problems.append((DEPLOYMENT_FILES_NAME, COMMAND_FILES_MESSAGE))
    wanted = [owner for owner in owners if owner in ticked]
    if wanted and docker_reason is not None:
        problems.append((COMMANDS_LABEL, f"{docker_reason} {UNAVAILABLE_SUFFIX}"))
        return commands, problems
    for owner in wanted:
        try:
            lines = parse_commands(posted.commands.get(owner, ""))
        except CommandError as error:
            problems.append((f"{COMMANDS_LABEL}: {owner}", str(error)))
            continue
        if not lines:
            problems.append((f"{COMMANDS_LABEL}: {owner}", EMPTY_COMMAND_MESSAGE))
            continue
        commands.append((owner, lines))
    return commands, problems


def review_import(
    scan: ComposeScan, posted: PostedImport, docker_reason: str | None = None
) -> Review:
    """Judge a posted choice against a fresh scan and build the selection to import.

    A service that is not in the scan or cannot be created is left out. A ticked file that is no
    longer offered or is disabled is a problem. Its owner is the posted one when that service is
    ticked and may own it, else the first of its services that is ticked, and the file is dropped
    when none is. A deployment file always belongs to the deployment files. A ticked command is
    judged by the safety net and needs Docker (``docker_reason`` is None when it is usable).
    """
    usable = usable_services(scan)
    chosen = tuple(name for name in usable if name in set(posted.services))
    by_key = {candidate.key: candidate for candidate in scan.files}
    own_by_key = {candidate.key: candidate for candidate in scan.deployment_files}
    picks: list[tuple[FileCandidate, FileChoice]] = []
    dropped: list[str] = []
    problems: list[tuple[str, str]] = []
    if not chosen:
        problems.append((SERVICES_LABEL, NO_SERVICE_MESSAGE))
    ticked = [(key, by_key.get(key), False) for key in posted.picked]
    ticked += [(key, own_by_key.get(key), True) for key in posted.deployment_picked]
    for key, candidate, own in ticked:
        if candidate is None:
            problems.append((key, GONE_MESSAGE))
            continue
        if candidate.disabled_reason is not None:
            problems.append((key, candidate.disabled_reason))
            continue
        if own:
            owner = DEPLOYMENT_FILES_NAME
        else:
            options = [name for name in owner_options(candidate, usable) if name in chosen]
            posted_owner = posted.owners.get(key, "")
            owner = posted_owner if posted_owner in options else (options[0] if options else "")
        if not owner:
            dropped.append(key)
            continue
        judged = _judge(candidate, owner, posted)
        if isinstance(judged, str):
            problems.append((key, judged))
            continue
        picks.append((candidate, judged))
    has_own = any(choice.owner == DEPLOYMENT_FILES_NAME for _, choice in picks)
    owners = [*chosen, *([DEPLOYMENT_FILES_NAME] if has_own else [])]
    commands, command_problems = _review_commands(posted, owners, docker_reason)
    problems.extend(command_problems)
    if scan.deployment_command_set and any(owner == DEPLOYMENT_FILES_NAME for owner, _ in commands):
        problems.append((DEPLOYMENT_FILES_NAME, COMMAND_STORED_MESSAGE))
    texts = tuple((owner, posted.commands.get(owner, "")) for owner, _ in commands)
    selection = ComposeSelection(
        scan.compose_files, chosen, tuple(choice for _, choice in picks), texts
    )
    return Review(selection, tuple(picks), tuple(dropped), tuple(problems), tuple(commands))


def selection_of(posted: PostedImport) -> ComposeSelection:
    """The selection a confirmed import posts, taken as it stands: the import matches every
    choice against its own fresh scan and refuses what does not fit."""
    choices: list[FileChoice] = []
    picked = [(key, posted.owners.get(key, "")) for key in posted.picked]
    picked += [(key, DEPLOYMENT_FILES_NAME) for key in posted.deployment_picked]
    for key, owner in picked:
        text = posted.types.get(key, "")
        try:
            file_type: FileType | None = FileType(text) if text else None
        except ValueError:
            file_type = None
        choices.append(
            FileChoice(
                key,
                owner,
                file_type,
                posted.dists.get(key) or None,
                bool(posted.starts.get(key)),
            )
        )
    commands = tuple(
        (owner, posted.commands.get(owner, "")) for owner in posted.runs if posted.runs[owner]
    )
    return ComposeSelection(posted.compose_files, posted.services, tuple(choices), commands)
