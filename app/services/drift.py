"""The drift view: the target on disk, the last deployed version and the managed version.

The three columns are compared per key. What a page loads holds names, presence marks and hash
prefixes only; decrypted content and the file read from the host live inside this module and a
value leaves it only through ``reveal_drift``, which writes its audit entries first. The target is
read at request time through the confined reader (allowed paths loaded fresh, no symlink followed
and every opened descriptor checked), and drift is decided by the same function and the same
baseline the deploy engine uses.

Importing the target saves its bytes as the next version; overwriting it is a forced deploy of that
one file through the engine, which keeps a copy of the file it replaces. No message here names a
host path or repeats operating system error text.
"""

import asyncio
import hmac
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics, telemetry
from app.core.crypto import CryptoService
from app.models import (
    DeployResultStatus,
    DriftState,
    EnvironmentVersion,
    PostDeployStatus,
    User,
)
from app.models.types import utc_now
from app.services.audit import AuditActor, record_environment_audit
from app.services.content import content_problem
from app.services.deploy import DeployEngine
from app.services.deploy_baseline import Baseline, load_baselines
from app.services.deploy_policy import drift_state
from app.services.deploy_types import DeployOutcome, DeployRequest
from app.services.editor import (
    STALE_MESSAGE,
    EditorHeader,
    audit_reveal,
    file_changed,
    stored_version,
)
from app.services.env_parser import parse_env
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    FileChange,
    announce_event,
)
from app.services.paths import load_allowed_paths
from app.services.permissions import AccessScope
from app.services.target_files import (
    PROBLEM_MESSAGES,
    TargetError,
    TargetProblem,
    locate_target,
    read_target_file,
)
from app.services.version_history import SHORT_HASH_LENGTH, WHOLE_FILE_KEY, DiffChange
from app.services.versions import create_version

IMPORT_COMMENT = "Imported from the target file"
NO_VERSION_MESSAGE = "No version is saved for this file."
TARGET_CHANGED_MESSAGE = "The target file changed since this page was opened. Reload the page."
COMPARISON_CHANGED_MESSAGE = (
    "The files changed since this page was opened. Reload the page to compare them again."
)
SAME_CONTENT_MESSAGE = (
    "The target already has the same content as the managed version. Nothing to import."
)
NOT_UTF8_MESSAGE = "The target file is not valid UTF-8 text, so it cannot be compared."
NOTHING_TO_OVERWRITE_MESSAGE = (
    "The target was not changed outside the app, so there is nothing to overwrite."
)
MISSING_MESSAGE = "The target file does not exist."
REVEAL_COLUMNS = ("target", "deployed", "managed")

_OUTSIDE_PROBLEMS = frozenset(
    {TargetProblem.NOT_RELATIVE, TargetProblem.ROOT_OUTSIDE, TargetProblem.LEAVES_ROOT}
)
_CHANGE_ORDER = {DiffChange.CHANGED: 0, DiffChange.ADDED: 1, DiffChange.REMOVED: 2}


class DriftKind(StrEnum):
    """How the target stands against what this app wrote and what it manages."""

    NO_DRIFT = "no_drift"
    DRIFT = "drift"
    NEVER_DEPLOYED = "never_deployed"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    OUTSIDE = "outside"


class Presence(StrEnum):
    """Whether a column holds a key: set, not set, or the column itself is not available."""

    SET = "set"
    NOT_SET = "not_set"
    UNAVAILABLE = "unavailable"


class DriftError(Exception):
    """An action was refused; the message is safe to show and never holds a value or a path."""


class DriftChangedError(DriftError):
    """What a page was built from changed, so a reveal answers no values."""


@dataclass(frozen=True)
class DriftRow:
    """A key that differs, by its position, with a mark per column and never a value.

    ``disk_change`` is how the target differs from the last deployed version (from the managed
    version when nothing was deployed); ``managed_change`` is how the managed version differs
    from the last deployed one, None when nothing was deployed.
    """

    index: int
    key: str
    target: Presence
    deployed: Presence
    managed: Presence
    disk_change: DiffChange | None
    managed_change: DiffChange | None


@dataclass(frozen=True)
class Comparison:
    """The rows that differ, how many keys did not and how the comparison was made.

    ``whole_file`` is set when a column is not env format, so the only row is the file itself.
    ``text_only`` means the texts differ but no variable does (comments, blank lines, line
    breaks or order). ``columns`` is how many of the three columns hold content; with fewer than
    two there is nothing to compare.
    """

    rows: tuple[DriftRow, ...]
    unchanged: int
    whole_file: bool
    text_only: bool
    columns: int


@dataclass(frozen=True)
class TargetColumn:
    """The target as read now: whether its content is shown, its hash prefix and read time."""

    readable: bool
    short_hash: str | None
    read_at: datetime


@dataclass(frozen=True)
class VersionColumn:
    """A stored version shown as a column: number, hash prefix and when it was saved or written."""

    number: int
    short_hash: str
    at: datetime


@dataclass(frozen=True)
class DriftView:
    """What the drift page shows, without any value.

    ``message`` explains the state in path-free words. ``target_hash`` is the keyed hash of the
    file as read, carried by the forms so an action can refuse when the file changed since.
    """

    kind: DriftKind
    message: str
    target: TargetColumn
    deployed: VersionColumn | None
    managed: VersionColumn
    comparison: Comparison
    target_hash: str | None
    can_import: bool
    can_overwrite: bool
    import_problem: str | None


@dataclass(frozen=True)
class RevealedDrift:
    """Revealed values by row index, then by column name; a column without a value is absent."""

    values: dict[int, dict[str, str]]


@dataclass(frozen=True)
class OverwriteResult:
    """What a successful overwrite did: the version written and what followed."""

    version_number: int
    unchanged: bool
    backed_up: bool
    post_deploy: PostDeployStatus


@dataclass(frozen=True)
class _Side:
    """One compared text with its variables, None when absent or not env format."""

    text: str | None
    values: dict[str, str] | None


@dataclass(frozen=True)
class _Entry:
    """A differing row with the values of its columns; private to this module."""

    row: DriftRow
    target: str | None
    deployed: str | None
    managed: str | None


@dataclass(frozen=True)
class _Snapshot:
    """The page's view plus the texts it was built from; never leaves this module."""

    view: DriftView
    target_text: str | None
    target_data: bytes | None
    deployed_text: str | None
    managed_text: str
    deployed_version: EnvironmentVersion | None
    managed_version: EnvironmentVersion


@dataclass(frozen=True)
class _TargetRead:
    """The outcome of reading the target: its bytes, or why there are none."""

    data: bytes | None
    problem: TargetProblem | None
    read_at: datetime


def _side(text: str | None) -> _Side:
    """A text with its variables; a text that is not env format has none."""
    if text is None:
        return _Side(None, None)
    parsed = parse_env(text)
    if not parsed.is_env_format:
        return _Side(text, None)
    return _Side(text, {key: variable.value for key, variable in parsed.variables.items()})


def _change(before: str | None, after: str | None) -> DiffChange | None:
    """How a value differs between two columns; None when equal or absent from both."""
    if before is None and after is None:
        return None
    if before is None:
        return DiffChange.ADDED
    if after is None:
        return DiffChange.REMOVED
    return DiffChange.CHANGED if before != after else None


def _presence(side: _Side, key: str) -> Presence:
    """Whether a column holds the key."""
    if side.text is None:
        return Presence.UNAVAILABLE
    return Presence.SET if side.values is not None and key in side.values else Presence.NOT_SET


def _whole_file_entries(
    target: _Side, deployed: _Side, managed: _Side, reference: _Side
) -> list[_Entry]:
    """The single File content row of a comparison that cannot go by key, empty when equal."""
    disk = DiffChange.CHANGED if target.text is not None and target.text != reference.text else None
    pending = (
        DiffChange.CHANGED if deployed.text is not None and managed.text != deployed.text else None
    )
    if disk is None and pending is None:
        return []

    def mark(side: _Side) -> Presence:
        return Presence.UNAVAILABLE if side.text is None else Presence.SET

    row = DriftRow(0, WHOLE_FILE_KEY, mark(target), mark(deployed), mark(managed), disk, pending)
    return [_Entry(row, target.text, deployed.text, managed.text)]


def _key_entries(target: _Side, deployed: _Side, managed: _Side, reference: _Side) -> list[_Entry]:
    """The keys that differ between the columns: on disk against the reference, managed against
    the last deployed version, disk differences first."""
    sides = (target, deployed, managed)
    keys = list(dict.fromkeys(key for side in sides for key in (side.values or {})))
    found: list[tuple[tuple[bool, int, int], _Entry]] = []
    for position, key in enumerate(keys):
        values = [side.values.get(key) if side.values is not None else None for side in sides]
        reference_value = reference.values.get(key) if reference.values is not None else None
        disk = _change(reference_value, values[0]) if target.text is not None else None
        pending = _change(values[1], values[2]) if deployed.text is not None else None
        if disk is None and pending is None:
            continue
        order = (disk is None, _CHANGE_ORDER[disk or pending or DiffChange.CHANGED], position)
        row = DriftRow(
            0,
            key,
            _presence(target, key),
            _presence(deployed, key),
            _presence(managed, key),
            disk,
            pending,
        )
        found.append((order, _Entry(row, values[0], values[1], values[2])))
    return [entry for _order, entry in sorted(found, key=lambda item: item[0])]


def _compare(
    target: str | None, deployed: str | None, managed: str
) -> tuple[list[_Entry], Comparison]:
    """The differing entries with their values and the value-free comparison of the columns.

    A column that is not env format makes the whole comparison go by file content, so no line
    of a certificate is ever listed as a key. Comments are never part of a result.
    """
    sides = (_side(target), _side(deployed), _side(managed))
    target_side, deployed_side, managed_side = sides
    reference = deployed_side if deployed_side.text is not None else managed_side
    whole_file = any(side.text is not None and side.values is None for side in sides)
    texts_differ = (target is not None and target != reference.text) or (
        deployed is not None and managed != deployed
    )
    columns = sum(side.text is not None for side in sides)
    found: list[_Entry]
    if columns < 2:
        found = []
        unchanged = 0
        whole_file = False
    elif whole_file:
        found = _whole_file_entries(target_side, deployed_side, managed_side, reference)
        unchanged = 0
    else:
        found = _key_entries(target_side, deployed_side, managed_side, reference)
        keys = {key for side in sides for key in (side.values or {})}
        unchanged = len(keys) - len(found)
    entries = [
        replace(entry, row=replace(entry.row, index=index)) for index, entry in enumerate(found)
    ]
    comparison = Comparison(
        rows=tuple(entry.row for entry in entries),
        unchanged=unchanged,
        whole_file=whole_file,
        text_only=not entries and texts_differ and not whole_file,
        columns=columns,
    )
    return entries, comparison


def compare_columns(target: str | None, deployed: str | None, managed: str) -> Comparison:
    """The value-free comparison of the three texts, None standing for a column without one."""
    return _compare(target, deployed, managed)[1]


async def _read_target(db: AsyncSession, root_path: str, target_path: str) -> _TargetRead:
    """Read the target now: allowed paths loaded fresh, the folder opened without links."""
    allowed = await load_allowed_paths(db)
    try:
        location = locate_target(allowed, root_path, target_path)
        data = await asyncio.to_thread(read_target_file, location, allowed)
    except TargetError as error:
        return _TargetRead(None, error.problem, utc_now())
    return _TargetRead(data, None, utc_now())


async def _deployed_version(
    db: AsyncSession, environment_id: int, baseline: Baseline | None
) -> EnvironmentVersion | None:
    """The version behind the baseline, None when nothing was deployed or the row is gone."""
    if baseline is None or baseline.version_id is None:
        return None
    version = await db.get(EnvironmentVersion, baseline.version_id)
    return version if version is not None and version.environment_id == environment_id else None


def _no_drift_message(
    target_hash: str, managed: EnvironmentVersion, baseline: Baseline | None
) -> str:
    """Why a target that is in sync is in sync."""
    if target_hash == managed.content_hash:
        return "The target file matches the managed version."
    if baseline is not None and target_hash == baseline.written_hash:
        return (
            "The target file matches the last deployed version. "
            f"Version {managed.number} is newer and waits for a deploy."
        )
    return "The target file is empty. The first deploy writes the managed version."


def _unreadable_kind(problem: TargetProblem) -> tuple[DriftKind, str]:
    """The state and the path-free sentence of a target that could not be read."""
    if problem is TargetProblem.FOLDER_MISSING:
        return DriftKind.MISSING, PROBLEM_MESSAGES[problem]
    if problem in _OUTSIDE_PROBLEMS:
        return DriftKind.OUTSIDE, PROBLEM_MESSAGES[problem]
    return DriftKind.UNREADABLE, PROBLEM_MESSAGES[problem]


async def _load(db: AsyncSession, crypto: CryptoService, header: EditorHeader) -> _Snapshot | None:
    """Everything the drift view needs, None when the file has no saved version.

    The deployment root comes from the header, which the request loaded already. A version is
    decrypted once even when the managed and the deployed version are the same one.
    """
    managed = await stored_version(db, header.id)
    if managed is None:
        return None
    baseline = (await load_baselines(db, [header.id])).get(header.id)
    deployed = await _deployed_version(db, header.id, baseline)
    read = await _read_target(db, header.deployment_root, header.target_path)
    managed_text = crypto.decrypt_text(managed.content_encrypted)
    deployed_text: str | None = None
    if deployed is not None:
        deployed_text = (
            managed_text
            if deployed.id == managed.id
            else crypto.decrypt_text(deployed.content_encrypted)
        )
    target_text: str | None = None
    target_hash: str | None = None
    if read.problem is not None:
        kind, message = _unreadable_kind(read.problem)
    elif read.data is None:
        kind, message = DriftKind.MISSING, MISSING_MESSAGE
    else:
        target_hash = crypto.content_hash(read.data)
        state = drift_state(
            read.data,
            target_hash,
            managed.content_hash,
            baseline.written_hash if baseline else None,
        )
        try:
            target_text = read.data.decode("utf-8")
        except UnicodeDecodeError:
            kind, message = DriftKind.UNREADABLE, NOT_UTF8_MESSAGE
        else:
            if state is DriftState.IN_SYNC:
                kind = DriftKind.NO_DRIFT
                message = _no_drift_message(target_hash, managed, baseline)
            elif baseline is None:
                kind = DriftKind.NEVER_DEPLOYED
                message = (
                    "This file was never deployed by the app and the target already has "
                    "content. Choose how to resolve it."
                )
            else:
                kind = DriftKind.DRIFT
                message = (
                    "The target file changed outside the app since the last deploy. "
                    "Choose how to resolve it."
                )
    _entries, comparison = _compare(target_text, deployed_text, managed_text)
    changed_outside = kind in (DriftKind.DRIFT, DriftKind.NEVER_DEPLOYED)
    import_problem = content_problem(target_text) if changed_outside and target_text else None
    view = DriftView(
        kind=kind,
        message=message,
        target=TargetColumn(
            readable=target_text is not None,
            short_hash=target_hash[:SHORT_HASH_LENGTH] if target_hash else None,
            read_at=read.read_at,
        ),
        deployed=(
            VersionColumn(
                deployed.number, deployed.content_hash[:SHORT_HASH_LENGTH], baseline.written_at
            )
            if deployed and baseline
            else None
        ),
        managed=VersionColumn(
            managed.number, managed.content_hash[:SHORT_HASH_LENGTH], managed.created_at
        ),
        comparison=comparison,
        target_hash=target_hash,
        can_import=changed_outside and import_problem is None,
        can_overwrite=changed_outside,
        import_problem=import_problem,
    )
    return _Snapshot(view, target_text, read.data, deployed_text, managed_text, deployed, managed)


async def load_drift_view(
    db: AsyncSession, crypto: CryptoService, header: EditorHeader
) -> DriftView | None:
    """The masked three-way view of a file, None when it has no version to compare.

    The load runs in a drift check span that carries ids and the state found; a target changed
    outside the app is counted as a detection.
    """
    ids = {
        telemetry.PROJECT_ID: header.project_id,
        telemetry.DEPLOYMENT_ID: header.deployment_id,
        telemetry.SERVICE_ID: header.service_id,
        telemetry.ENVIRONMENT_ID: header.id,
    }
    with telemetry.span(telemetry.DRIFT_CHECK_SPAN, ids) as active:
        snapshot = await _load(db, crypto, header)
        if snapshot is None:
            return None
        kind = snapshot.view.kind
        telemetry.annotate(active, {telemetry.DRIFT_STATE: kind.value})
    if kind in (DriftKind.DRIFT, DriftKind.NEVER_DEPLOYED):
        metrics.record_drift_detection("view")
    return snapshot.view


def _same_hash(posted: str | None, current: str | None) -> bool:
    """Whether the hash a form carried is the hash of the file now (both absent counts)."""
    return hmac.compare_digest((posted or "").encode(), (current or "").encode())


async def reveal_drift(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    *,
    actor: User,
    ip_address: str | None,
    columns: set[str],
    managed_number: int,
    deployed_number: int | None,
    target_hash: str | None,
) -> RevealedDrift:
    """The values of the differing keys of the chosen columns, audited before they are returned.

    The numbers and the hash are those the masked page was built from; the file is read again
    and nothing is returned when any of them changed, so a value never lands next to another
    key. Each column that hands out a value writes its own entry (the target one has no version).
    Only the keys of the listed rows are revealed, never the unchanged ones.
    """
    snapshot = await _load(db, crypto, header)
    if snapshot is None:
        raise DriftError(NO_VERSION_MESSAGE)
    view = snapshot.view
    if (
        managed_number != view.managed.number
        or deployed_number != (view.deployed.number if view.deployed else None)
        or not _same_hash(target_hash, view.target_hash)
    ):
        raise DriftChangedError(COMPARISON_CHANGED_MESSAGE)
    entries, comparison = _compare(
        snapshot.target_text, snapshot.deployed_text, snapshot.managed_text
    )
    values: dict[int, dict[str, str]] = {}
    for column, version in (
        ("target", None),
        ("deployed", snapshot.deployed_version),
        ("managed", snapshot.managed_version),
    ):
        if column not in columns:
            continue
        shown = [(entry.row, getattr(entry, column)) for entry in entries]
        shown = [(row, value) for row, value in shown if value is not None]
        if not shown:
            continue
        details: dict[str, object] = {"scope": "file" if comparison.whole_file else "drift"}
        details["column"] = column
        if not comparison.whole_file:
            details["keys"] = [row.key for row, _value in shown]
        await audit_reveal(
            db, header, AuditActor.for_user(actor, ip_address), details, version=version
        )
        for row, value in shown:
            values.setdefault(row.index, {})[column] = value
    return RevealedDrift(values)


async def import_target(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    *,
    actor: User,
    ip_address: str | None,
    base_number: int,
    target_hash: str,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> EnvironmentVersion:
    """Save the target as read now, byte for byte, as the next version of the file.

    Refused with ``DriftError`` when the page was built from an older managed version, when the
    file on disk changed since the page was rendered (compared by keyed hash), when it cannot be
    read, is not UTF-8 text or cannot be stored, or when it equals the managed version. Nothing
    is saved then. The comment is automatic and the audit entry is committed with the version.
    """
    snapshot = await _load(db, crypto, header)
    if snapshot is None:
        raise DriftError(NO_VERSION_MESSAGE)
    if snapshot.managed_version.number != base_number:
        raise DriftError(STALE_MESSAGE)
    view = snapshot.view
    if snapshot.target_data is None or view.target_hash is None:
        raise DriftError(view.message)
    if not _same_hash(target_hash, view.target_hash):
        raise DriftError(TARGET_CHANGED_MESSAGE)
    if snapshot.target_text is None:
        raise DriftError(NOT_UTF8_MESSAGE)
    if view.target_hash == snapshot.managed_version.content_hash:
        raise DriftError(SAME_CONTENT_MESSAGE)
    problem = content_problem(snapshot.target_text)
    if problem:
        raise DriftError(problem)
    try:
        version = await create_version(
            db,
            crypto,
            header.id,
            snapshot.target_text,
            author=actor,
            comment=IMPORT_COMMENT,
        )
        await record_environment_audit(
            db,
            AuditActor.for_user(actor, ip_address),
            header,
            action="environment.import",
            details={"number": version.number, "source": "target"},
            version_ids=[version.id],
        )
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise DriftError(STALE_MESSAGE) from error
    await announce_event(notifier, file_changed(header, version.number, FileChange.IMPORTED, actor))
    return version


async def overwrite_target(
    db: AsyncSession,
    engine: DeployEngine,
    scope: AccessScope,
    actor: AuditActor,
    header: EditorHeader,
    *,
    base_number: int,
) -> OverwriteResult:
    """Replace the target with the managed version the page showed, as a forced deploy.

    The deploy goes through the engine for this one file, forced and with a copy of the drifted
    file kept next to it, so the checks, the lock, the audit entry and the post-deploy step are
    those of any deploy. Refused with ``DriftError`` when the page is out of date, when the
    target was not changed outside the app or cannot be replaced, and when the engine reports
    anything but a write. ``DeployRequestError`` from the engine passes through.
    """
    latest = await stored_version(db, header.id)
    if latest is None or latest.number != base_number:
        raise DriftError(STALE_MESSAGE)
    snapshot = await _load(db, engine.crypto, header)
    if snapshot is None:
        raise DriftError(NO_VERSION_MESSAGE)
    view = snapshot.view
    if not view.can_overwrite:
        raise DriftError(
            NOTHING_TO_OVERWRITE_MESSAGE if view.kind is DriftKind.NO_DRIFT else view.message
        )
    outcome: DeployOutcome = await engine.run(
        db,
        scope,
        actor,
        DeployRequest(
            service_id=header.service_id,
            environment_ids=(header.id,),
            versions={header.id: base_number},
            force=True,
            backup_drifted=True,
        ),
    )
    if outcome.refusal is not None:
        raise DriftError(outcome.message or "The deploy did not start.")
    result = next((item for item in outcome.environments if item.environment_id == header.id), None)
    if result is None:
        raise DriftError("The file was not deployed.")
    if result.status not in (DeployResultStatus.WRITTEN, DeployResultStatus.UNCHANGED):
        raise DriftError(result.message)
    return OverwriteResult(
        version_number=base_number,
        unchanged=result.status is DeployResultStatus.UNCHANGED,
        backed_up=result.backup_path is not None,
        post_deploy=outcome.post_deploy.status,
    )
