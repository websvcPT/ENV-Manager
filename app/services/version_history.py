"""Version history of an environment: the version list, key level diffs, audited reveals, restore.

Decrypted content lives only inside these functions. What a page loads is version numbers,
authors, times, comments, hash prefixes and the names of the keys that differ. A value leaves
this module only through ``reveal_diff``, which writes its audit entries first; comments and
other text of the files are never part of a result, and nothing here puts a value in an audit
entry or an error message.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import EnvironmentVersion, FileType, User
from app.services.audit import AuditActor, record_environment_audit
from app.services.editor import (
    STALE_MESSAGE,
    EditorHeader,
    audit_reveal,
    file_changed,
    stored_version,
)
from app.services.env_parser import parse_env
from app.services.file_types import aligned_lines, content_problem_for, line_name
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    FileChange,
    announce_event,
)
from app.services.versions import create_version

SHORT_HASH_LENGTH = 7
WHOLE_FILE_KEY = "File content"


class DiffChange(StrEnum):
    """How a key differs between the two compared versions."""

    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"


class VersionNotFoundError(Exception):
    """A requested version does not exist for the environment."""


class RestoreError(Exception):
    """A restore was refused; the message is safe to show and never holds a value."""


@dataclass(frozen=True)
class VersionRow:
    """One saved version as the history table lists it; ``deployed`` marks the deployed one."""

    number: int
    author: str | None
    created_at: datetime
    comment: str | None
    short_hash: str
    deployed: bool


@dataclass(frozen=True)
class DiffRow:
    """A key that differs, by its position in the diff, never with a value."""

    index: int
    key: str
    change: DiffChange


@dataclass(frozen=True)
class VersionDiff:
    """The difference of two versions, ``left`` older than ``right``.

    ``whole_file`` is set when either side is not env format, so the only row is the file
    itself. ``by_line`` is set for a certificate, whose rows are lines (``Line 3``) instead of
    keys. ``identical`` means the two versions hold the same content. Keys that did not change
    are counted, not listed.
    """

    left: int
    right: int
    rows: tuple[DiffRow, ...]
    unchanged: int
    whole_file: bool
    identical: bool
    by_line: bool = False


@dataclass(frozen=True)
class RevealedDiff:
    """Values of the differing rows by row index: (left value, right value), None when absent."""

    values: dict[int, tuple[str | None, str | None]]


@dataclass(frozen=True)
class _Entry:
    """A differing key with both values; private to this module."""

    key: str
    change: DiffChange
    left: str | None
    right: str | None


def default_pair(numbers: Sequence[int]) -> tuple[int, int] | None:
    """The two newest versions, older first; None when fewer than two exist."""
    newest = sorted(numbers)[-2:]
    return (newest[0], newest[1]) if len(newest) == 2 else None


def pick_pair(requested: Sequence[int], numbers: Sequence[int]) -> tuple[int, int] | None:
    """The selected pair, older first; None unless exactly two distinct versions that exist."""
    chosen = sorted(set(requested))
    if len(chosen) != 2 or not set(chosen) <= set(numbers):
        return None
    return chosen[0], chosen[1]


async def load_versions(db: AsyncSession, header: EditorHeader) -> tuple[VersionRow, ...]:
    """Every version of the environment, newest first, without any content."""
    rows = (
        await db.execute(
            select(
                EnvironmentVersion.number,
                User.name,
                EnvironmentVersion.created_at,
                EnvironmentVersion.comment,
                EnvironmentVersion.content_hash,
            )
            .outerjoin(User, User.id == EnvironmentVersion.author_id)
            .where(EnvironmentVersion.environment_id == header.id)
            .order_by(EnvironmentVersion.number.desc())
        )
    ).all()
    return tuple(
        VersionRow(
            number=number,
            author=author,
            created_at=created_at,
            comment=comment,
            short_hash=content_hash[:SHORT_HASH_LENGTH],
            deployed=number == header.deployed,
        )
        for number, author, created_at, comment, content_hash in rows
    )


def _compare_lines(left_text: str, right_text: str) -> tuple[list[_Entry], int]:
    """The differing lines of a certificate in file order and the count of equal ones.

    The lines are aligned, so a line inserted in the middle is one added line and the lines after
    it stay unchanged. A row is named by the number of its line in the newer text, a removed line
    by its number in the older one.
    """
    entries: list[_Entry] = []
    unchanged = 0
    for step in aligned_lines(left_text, right_text):
        if step.left == step.right:
            unchanged += 1
        elif step.left is None and step.right_number is not None:
            entries.append(_Entry(line_name(step.right_number), DiffChange.ADDED, None, step.right))
        elif step.right is None and step.left_number is not None:
            entries.append(_Entry(line_name(step.left_number), DiffChange.REMOVED, step.left, None))
        elif step.right_number is not None:
            entries.append(
                _Entry(line_name(step.right_number), DiffChange.CHANGED, step.left, step.right)
            )
    return entries, unchanged


def _compare(
    left_text: str, right_text: str, file_type: FileType
) -> tuple[list[_Entry], int, bool]:
    """The differing entries (changed, then added, then removed), the unchanged key count and
    whether the comparison fell back to the whole file.

    A certificate is compared by line instead: the entries are named by line and come in file
    order, and the comparison never falls back to the whole file.
    """
    if file_type is FileType.CERT:
        entries, unchanged = _compare_lines(left_text, right_text)
        return entries, unchanged, False
    left = parse_env(left_text)
    right = parse_env(right_text)
    if not (left.is_env_format and right.is_env_format):
        if left_text == right_text:
            return [], 0, True
        return [_Entry(WHOLE_FILE_KEY, DiffChange.CHANGED, left_text, right_text)], 0, True
    changed: list[_Entry] = []
    added: list[_Entry] = []
    removed: list[_Entry] = []
    unchanged = 0
    for key, variable in right.variables.items():
        before = left.variables.get(key)
        if before is None:
            added.append(_Entry(key, DiffChange.ADDED, None, variable.value))
        elif before.value != variable.value:
            changed.append(_Entry(key, DiffChange.CHANGED, before.value, variable.value))
        else:
            unchanged += 1
    for key, variable in left.variables.items():
        if key not in right.variables:
            removed.append(_Entry(key, DiffChange.REMOVED, variable.value, None))
    return [*changed, *added, *removed], unchanged, False


async def _pair(
    db: AsyncSession, header: EditorHeader, left: int, right: int
) -> tuple[EnvironmentVersion, EnvironmentVersion]:
    """The two stored versions of this environment, older first, or ``VersionNotFoundError``."""
    first, second = sorted((left, right))
    older = await stored_version(db, header.id, first)
    newer = await stored_version(db, header.id, second)
    if older is None or newer is None or first == second:
        raise VersionNotFoundError
    return older, newer


async def load_diff(
    db: AsyncSession, crypto: CryptoService, header: EditorHeader, left: int, right: int
) -> VersionDiff:
    """The masked key level diff of two versions; values are compared but not kept."""
    older, newer = await _pair(db, header, left, right)
    entries, unchanged, whole_file = _compare(
        crypto.decrypt_text(older.content_encrypted),
        crypto.decrypt_text(newer.content_encrypted),
        header.file_type,
    )
    return VersionDiff(
        left=older.number,
        right=newer.number,
        rows=tuple(DiffRow(index, entry.key, entry.change) for index, entry in enumerate(entries)),
        unchanged=unchanged,
        whole_file=whole_file,
        identical=older.content_hash == newer.content_hash,
        by_line=header.file_type is FileType.CERT,
    )


async def reveal_diff(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    left: int,
    right: int,
    *,
    actor: User,
    ip_address: str | None,
) -> RevealedDiff:
    """The values of the differing keys of both sides, after one reveal is audited per version.

    Only the keys listed in the diff are revealed, never the unchanged ones. A side that holds
    no value (a key that was added or removed) writes no entry.
    """
    older, newer = await _pair(db, header, left, right)
    entries, _unchanged, whole_file = _compare(
        crypto.decrypt_text(older.content_encrypted),
        crypto.decrypt_text(newer.content_encrypted),
        header.file_type,
    )
    for stored, other, side in (
        (older, newer, [entry.key for entry in entries if entry.left is not None]),
        (newer, older, [entry.key for entry in entries if entry.right is not None]),
    ):
        if not side:
            continue
        details: dict[str, object] = (
            {"scope": "file", "compared_with": other.number}
            if whole_file
            else {"scope": "diff", "compared_with": other.number, "keys": side}
        )
        await audit_reveal(
            db, header, AuditActor.for_user(actor, ip_address), details, version=stored
        )
    return RevealedDiff(
        values={index: (entry.left, entry.right) for index, entry in enumerate(entries)}
    )


async def restore_version(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    number: int,
    *,
    base_number: int,
    actor: User,
    ip_address: str | None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> EnvironmentVersion:
    """Save the content of version ``number`` exactly as it is as the next version.

    Refused with ``RestoreError`` when the page was loaded before a newer version was saved or
    when the content already is the current version's. Raises ``VersionNotFoundError`` for an
    unknown version. The older versions stay in the history.
    """
    latest = await stored_version(db, header.id)
    if latest is None or latest.number != base_number:
        raise RestoreError(STALE_MESSAGE)
    source = await stored_version(db, header.id, number)
    if source is None:
        raise VersionNotFoundError
    if source.content_hash == latest.content_hash:
        raise RestoreError(
            f"Version {number} has the same content as the current version. Nothing to restore."
        )
    text = crypto.decrypt_text(source.content_encrypted)
    problem = content_problem_for(header.file_type, text)
    if problem:
        raise RestoreError(f"Version {number} cannot be restored. {problem}")
    try:
        version = await create_version(
            db, crypto, header.id, text, author=actor, comment=f"Restored from version {number}"
        )
        await record_environment_audit(
            db,
            AuditActor.for_user(actor, ip_address),
            header,
            action="environment.restore",
            details={"number": version.number, "restored": number},
            version_ids=[source.id, version.id],
        )
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise RestoreError(STALE_MESSAGE) from error
    await announce_event(notifier, file_changed(header, version.number, FileChange.RESTORED, actor))
    return version
