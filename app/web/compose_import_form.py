"""What the Docker Compose import screens post, and how a fresh scan judges it.

The screens carry the user's choices in the form and never keep the scan: every step reads the
Compose files again, so what a step shows or imports is what the files hold now. The posted names
are only choices, matched against the fresh scan by key. Files are named by their key in the
fields (``owner:<key>``, ``type:<key>``, ``dist:<key>``, ``start:<key>``), not by their position,
so a scan that lists them in another order does not shift a choice onto another file.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from starlette.datastructures import FormData

from app.models import FileType
from app.services.compose_import import (
    DIST_NEEDED_MESSAGE,
    NO_SERVICE_MESSAGE,
    ComposeSelection,
    FileChoice,
)
from app.services.compose_scan import ComposeScan, FileCandidate

OWNER_PREFIX = "owner:"
TYPE_PREFIX = "type:"
DIST_PREFIX = "dist:"
START_PREFIX = "start:"
GONE_MESSAGE = "This file is no longer found in the Compose files."
SERVICES_LABEL = "Services"


@dataclass(frozen=True)
class PostedImport:
    """The fields of an import step: the Compose files, the ticked services, the ticked files by
    key and the owner, type, dist path and start-from-dist choice posted for each key."""

    compose_files: tuple[str, ...]
    services: tuple[str, ...]
    picked: tuple[str, ...]
    owners: dict[str, str]
    types: dict[str, str]
    dists: dict[str, str]
    starts: dict[str, str]


@dataclass(frozen=True)
class FileRow:
    """One file of the preview: its candidate, the services that may own it, the owner, type and
    dist choices it starts with, and whether it starts ticked. ``from_dist`` ticks both the dist
    link and starting from the dist file, for a file that is not on disk."""

    candidate: FileCandidate
    owner_options: tuple[str, ...]
    owner: str
    file_type: str
    checked: bool
    from_dist: bool


@dataclass(frozen=True)
class Review:
    """A posted choice judged against a fresh scan.

    ``selection`` is what the import takes, built only from the scan; ``picks`` are the files in
    it with their candidates; ``dropped`` are ticked files left out because none of the services
    that use them is ticked; ``problems`` are (label, message) pairs, empty when it can go on.
    """

    selection: ComposeSelection
    picks: tuple[tuple[FileCandidate, FileChoice], ...]
    dropped: tuple[str, ...]
    problems: tuple[tuple[str, str], ...]


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
        )
        for candidate in scan.files
    )


def _file_type(text: str, candidate: FileCandidate) -> FileType:
    """The posted type, the candidate's own when the text is not a file type."""
    try:
        return FileType(text)
    except ValueError:
        return candidate.file_type


def review_import(scan: ComposeScan, posted: PostedImport) -> Review:
    """Judge a posted choice against a fresh scan and build the selection to import.

    A service that is not in the scan or cannot be created is left out. A ticked file that is no
    longer offered or is disabled is a problem. Its owner is the posted one when that service is
    ticked and may own it, else the first of its services that is ticked, and the file is dropped
    when none is. The dist file is the fresh scan's suggestion, taken only for an ENV file; a file
    that is not on disk starts from it only when it is linked.
    """
    usable = usable_services(scan)
    chosen = tuple(name for name in usable if name in set(posted.services))
    by_key = {candidate.key: candidate for candidate in scan.files}
    picks: list[tuple[FileCandidate, FileChoice]] = []
    dropped: list[str] = []
    problems: list[tuple[str, str]] = []
    if not chosen:
        problems.append((SERVICES_LABEL, NO_SERVICE_MESSAGE))
    for key in posted.picked:
        candidate = by_key.get(key)
        if candidate is None:
            problems.append((key, GONE_MESSAGE))
            continue
        if candidate.disabled_reason is not None:
            problems.append((key, candidate.disabled_reason))
            continue
        options = [name for name in owner_options(candidate, usable) if name in chosen]
        posted_owner = posted.owners.get(key, "")
        owner = posted_owner if posted_owner in options else (options[0] if options else "")
        if not owner:
            dropped.append(key)
            continue
        file_type = _file_type(posted.types.get(key, ""), candidate)
        if file_type.value in candidate.type_problems:
            problems.append((key, candidate.type_problems[file_type.value]))
            continue
        wants_dist = bool(posted.dists.get(key)) and file_type is FileType.ENV
        dist_path = candidate.dist_suggestion if wants_dist else None
        from_dist = bool(posted.starts.get(key)) and not candidate.exists
        if from_dist and dist_path is None:
            problems.append((key, DIST_NEEDED_MESSAGE))
            continue
        picks.append((candidate, FileChoice(key, owner, file_type, dist_path, from_dist)))
    selection = ComposeSelection(scan.compose_files, chosen, tuple(choice for _, choice in picks))
    return Review(selection, tuple(picks), tuple(dropped), tuple(problems))


def selection_of(posted: PostedImport) -> ComposeSelection:
    """The selection a confirmed import posts, taken as it stands: the import matches every
    choice against its own fresh scan and refuses what does not fit."""
    choices: list[FileChoice] = []
    for key in posted.picked:
        text = posted.types.get(key, "")
        try:
            file_type: FileType | None = FileType(text) if text else None
        except ValueError:
            file_type = None
        choices.append(
            FileChoice(
                key,
                posted.owners.get(key, ""),
                file_type,
                posted.dists.get(key) or None,
                bool(posted.starts.get(key)),
            )
        )
    return ComposeSelection(posted.compose_files, posted.services, tuple(choices))
