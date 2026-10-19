"""The file editor: what a caller sees of one environment, audited reveals and saving versions.

Decrypted content lives only inside these functions. What the page loads is the header and the
variable names; a value leaves this module only through ``reveal_value`` and
``reveal_content``, which write the audit entry first, and nothing here ever puts a value in an
audit entry or an error message.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    DeployResult,
    DeployResultStatus,
    DeployRun,
    EnvironmentVersion,
    User,
)
from app.services.audit import AuditActor, record_environment_audit
from app.services.content import content_problem, normalize_newlines
from app.services.env_parser import parse_env
from app.services.env_text import VARIABLE_NAME, EnvEditError, VariableChanges, apply_changes
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    FileChange,
    FileChanged,
    announce_event,
)
from app.services.paths import AllowedPaths
from app.services.permissions import (
    AccessScope,
    Capability,
    ItemLevel,
    ItemRef,
    Visibility,
)
from app.services.service_detail import load_environment_rows, load_service_header
from app.services.structure_lookup import ServiceChain, environment_chain
from app.services.versions import create_version

MAX_COMMENT_LENGTH = 500
STALE_MESSAGE = "This file changed since you opened it. Reload the page to continue."
NO_CHANGES_MESSAGE = "No changes to save."
NOT_ENV_MESSAGE = "This file is not in KEY=VALUE format. Edit it as raw text."
_WRITTEN = (DeployResultStatus.WRITTEN, DeployResultStatus.UNCHANGED)


class EditorError(Exception):
    """A save was refused; the message is safe to show and never holds a value."""


class VariableNotFoundError(Exception):
    """The requested variable does not exist in the version."""


@dataclass(frozen=True)
class EditorHeader:
    """One environment as the caller sees it.

    ``current`` is the newest saved version and ``deployed`` the version of the newest
    successful write, None when nothing was written yet. ``status`` is a badge key.
    File mode, owner and group are the effective values (the environment's own or the
    deployment default); an empty owner or group means the application user.
    ``can_manage_grants`` is True for the superadmin, who gets the Access tab. ``deployment_root``
    is the folder of the deployment for the code that reads the host; no page shows it.
    """

    id: int
    name: str
    service_id: int
    service_name: str
    deployment_id: int
    deployment_name: str
    project_id: int
    project_name: str
    target_path: str
    dist_path: str | None
    file_mode: str
    file_owner: str
    file_group: str
    status: str
    current: int | None
    current_at: datetime | None
    current_author: str | None
    deployed: int | None
    deployed_at: datetime | None
    can_edit: bool
    can_reveal: bool
    can_check_dist: bool
    item: ItemRef
    can_manage_grants: bool
    deployment_root: str = field(repr=False)

    @property
    def can_deploy(self) -> bool:
        """Whether the caller may deploy this file; checking its dist uses the same capability."""
        return self.can_check_dist


@dataclass(frozen=True)
class VariableRow:
    """A variable as the page lists it: the name and the size of its value, never the value."""

    key: str
    index: int
    lines: int

    @property
    def multiline(self) -> bool:
        """Whether the value spans several lines (a certificate or key)."""
        return self.lines > 1


@dataclass(frozen=True)
class EditorContent:
    """What a masked render needs of the latest version: names and sizes only."""

    number: int
    env_format: bool
    rows: tuple[VariableRow, ...]
    line_count: int


@dataclass(frozen=True)
class KeyValueSubmission:
    """The submitted key/value edits: replaced values, removed keys and added variables."""

    replaced: Sequence[tuple[str, str]] = ()
    removed: Sequence[str] = ()
    added: Sequence[tuple[str, str]] = ()


async def load_editor_header(
    db: AsyncSession,
    scope: AccessScope,
    environment_id: int,
    visibility: Visibility | None = None,
    allowed: AllowedPaths | None = None,
) -> EditorHeader | None:
    """The environment for the caller, or None when it does not exist or is outside the scope.

    ``visibility`` and ``allowed`` are the ones the request already holds; without them they
    are loaded here.
    """
    chain = await environment_chain(db, environment_id)
    if chain is None:
        return None
    environment, deployment = chain.environment, chain.deployment
    service = await load_service_header(
        db,
        scope,
        chain.service.id,
        visibility,
        allowed,
        chain=ServiceChain(chain.service, deployment, chain.project_name),
    )
    if service is None or environment_id not in service.visible_environment_ids:
        return None
    rows = await load_environment_rows(db, scope, service)
    row = next((row for row in rows if row.id == environment_id), None)
    if row is None:
        return None
    item = ItemRef(
        level=ItemLevel.ENVIRONMENT,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=chain.service.id,
        environment_id=environment_id,
    )
    latest = (
        await db.execute(
            select(EnvironmentVersion, User.name)
            .outerjoin(User, User.id == EnvironmentVersion.author_id)
            .where(EnvironmentVersion.environment_id == environment_id)
            .order_by(EnvironmentVersion.number.desc())
            .limit(1)
        )
    ).first()
    written = (
        await db.execute(
            select(EnvironmentVersion.number, DeployRun.started_at)
            .join(DeployResult, DeployResult.version_id == EnvironmentVersion.id)
            .join(DeployRun, DeployRun.id == DeployResult.run_id)
            .where(
                DeployResult.environment_id == environment_id,
                DeployRun.dry_run.is_(False),
                DeployResult.status.in_(_WRITTEN),
            )
            .order_by(DeployRun.started_at.desc(), DeployResult.id.desc())
            .limit(1)
        )
    ).first()
    return EditorHeader(
        id=environment.id,
        name=environment.name,
        service_id=service.id,
        service_name=service.name,
        deployment_id=deployment.id,
        deployment_name=deployment.name,
        project_id=service.project_id,
        project_name=service.project_name,
        target_path=environment.target_path,
        dist_path=environment.dist_path,
        file_mode=environment.file_mode or deployment.file_mode,
        file_owner=(
            environment.file_owner if environment.file_owner is not None else deployment.file_owner
        )
        or "",
        file_group=(
            environment.file_group if environment.file_group is not None else deployment.file_group
        )
        or "",
        status=row.status,
        current=latest[0].number if latest else None,
        current_at=latest[0].created_at if latest else None,
        current_author=latest[1] if latest else None,
        deployed=written[0] if written else None,
        deployed_at=written[1] if written else None,
        can_edit=row.can_edit,
        can_reveal=scope.can(Capability.REVEAL_SECRETS, item),
        can_check_dist=row.can_check_dist,
        item=item,
        can_manage_grants=scope.can(Capability.MANAGE_GRANTS),
        deployment_root=deployment.root_path,
    )


async def stored_version(
    db: AsyncSession, environment_id: int, number: int | None = None
) -> EnvironmentVersion | None:
    """A saved version of the environment: the one with this number, else the newest."""
    stmt = select(EnvironmentVersion).where(EnvironmentVersion.environment_id == environment_id)
    stmt = (
        stmt.where(EnvironmentVersion.number == number)
        if number is not None
        else stmt.order_by(EnvironmentVersion.number.desc()).limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _line_count(text: str) -> int:
    """The number of lines of a text, counting a last line without a line break."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


async def load_editor_content(
    db: AsyncSession, crypto: CryptoService, environment_id: int
) -> EditorContent | None:
    """Names and sizes of the newest version, or None when nothing was saved yet."""
    version = await stored_version(db, environment_id)
    if version is None:
        return None
    text = crypto.decrypt_text(version.content_encrypted)
    parsed = parse_env(text)
    rows = tuple(
        VariableRow(key=key, index=index, lines=variable.value.count("\n") + 1)
        for index, (key, variable) in enumerate(parsed.variables.items())
    )
    return EditorContent(
        number=version.number,
        env_format=parsed.is_env_format,
        rows=rows if parsed.is_env_format else (),
        line_count=_line_count(text),
    )


async def variable_row(
    db: AsyncSession, crypto: CryptoService, environment_id: int, key: str
) -> VariableRow | None:
    """The masked row of one variable of the newest version, None when it does not exist."""
    content = await load_editor_content(db, crypto, environment_id)
    if content is None:
        return None
    return next((row for row in content.rows if row.key == key), None)


async def audit_reveal(
    db: AsyncSession,
    header: EditorHeader,
    actor: AuditActor,
    details: dict[str, object],
    *,
    version: EnvironmentVersion | None = None,
) -> None:
    """Write and commit the audit entry of a reveal before the value is handed out.

    ``version`` is the stored version that is revealed; a reveal of the file as it was read from
    the host has none.
    """
    if version is not None:
        details = {"version": version.number, **details}
    await record_environment_audit(
        db,
        actor,
        header,
        action="environment.reveal",
        details=details,
        version_ids=[version.id] if version is not None else (),
    )
    await db.commit()


async def reveal_value(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    key: str,
    *,
    actor: User,
    ip_address: str | None,
    version: int | None = None,
) -> str:
    """The value of one variable, after the reveal is audited.

    ``version`` defaults to the newest. Raises ``VariableNotFoundError`` for an unknown version
    or key; nothing is audited then.
    """
    stored = await stored_version(db, header.id, version)
    if stored is None:
        raise VariableNotFoundError
    parsed = parse_env(crypto.decrypt_text(stored.content_encrypted))
    variable = parsed.variables.get(key) if parsed.is_env_format else None
    if variable is None:
        raise VariableNotFoundError
    await audit_reveal(
        db, header, AuditActor.for_user(actor, ip_address), {"key": key}, version=stored
    )
    return variable.value


async def reveal_content(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    *,
    actor: User,
    ip_address: str | None,
    version: int | None = None,
) -> str:
    """The whole text of a version, after the reveal is audited."""
    stored = await stored_version(db, header.id, version)
    if stored is None:
        raise VariableNotFoundError
    await audit_reveal(
        db, header, AuditActor.for_user(actor, ip_address), {"scope": "file"}, version=stored
    )
    return crypto.decrypt_text(stored.content_encrypted)


def _clean_comment(comment: str) -> str | None:
    """The trimmed comment, None when empty; raises when it is longer than the column."""
    text = comment.strip()
    if len(text) > MAX_COMMENT_LENGTH:
        raise EditorError(f"The comment is longer than {MAX_COMMENT_LENGTH} characters.")
    return text or None


def _key_value_text(current: str, submission: KeyValueSubmission) -> tuple[str, list[str]]:
    """The text after the submitted edits and the names of the variables they touch.

    The stored text is normalized to LF first, as a raw save would be, so the same final text
    stores the same content whichever editor produced it. Edits that change no variable are
    refused even when the stored text had other line breaks.
    """
    base = normalize_newlines(current)
    parsed = parse_env(base)
    if not parsed.is_env_format:
        raise EditorError(NOT_ENV_MESSAGE)
    values: dict[str, str] = {}
    for key, value in submission.replaced:
        if key not in parsed.variables:
            raise EditorError("A variable you edited no longer exists. Reload the page.")
        if value:
            values[key] = normalize_newlines(value)
    removed = set(submission.removed)
    if not removed <= parsed.variables.keys():
        raise EditorError("A variable you removed no longer exists. Reload the page.")
    added: set[str] = set()
    for key, value in submission.added:
        name = key.strip()
        if not VARIABLE_NAME.fullmatch(name):
            raise EditorError("Use letters, numbers, dots, dashes and underscores in a name.")
        if name in parsed.variables or name in added:
            raise EditorError(f"{name} already exists.")
        added.add(name)
        values[name] = normalize_newlines(value)
    try:
        text = apply_changes(base, VariableChanges(values=values, remove=removed))
    except EnvEditError as error:
        raise EditorError(str(error)) from error
    if text == base:
        raise EditorError(NO_CHANGES_MESSAGE)
    return text, sorted(set(values) | removed)


def file_changed(header: EditorHeader, number: int, change: FileChange, actor: User) -> FileChanged:
    """The event for a new version of the file, saved by the user."""
    return FileChanged(
        header.project_id,
        header.deployment_id,
        header.service_id,
        header.id,
        header.name,
        number,
        change,
        actor.name,
    )


async def save_version(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    *,
    actor: User,
    ip_address: str | None,
    base_number: int,
    comment: str,
    raw_content: str | None = None,
    key_values: KeyValueSubmission | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> EnvironmentVersion:
    """Save the edit as the next version.

    Exactly one of ``raw_content`` (the whole text) and ``key_values`` is given. Both end in
    the same normalized text and the same version insert. Refused with ``EditorError`` when the
    page was loaded from an older version, nothing changed or the text cannot be stored.
    """
    latest = await stored_version(db, header.id)
    if latest is None or latest.number != base_number:
        raise EditorError(STALE_MESSAGE)
    clean_comment = _clean_comment(comment)
    current = crypto.decrypt_text(latest.content_encrypted)
    changed_keys: list[str] | None = None
    if raw_content is not None:
        text = normalize_newlines(raw_content)
    elif key_values is not None:
        text, changed_keys = _key_value_text(current, key_values)
    else:
        raise EditorError("Reveal the raw text to edit it.")
    problem = content_problem(text)
    if problem:
        raise EditorError(problem)
    if text == current:
        raise EditorError(NO_CHANGES_MESSAGE)
    try:
        version = await create_version(
            db, crypto, header.id, text, author=actor, comment=clean_comment
        )
        details: dict[str, object] = {
            "number": version.number,
            "mode": "raw" if raw_content is not None else "key_value",
        }
        if changed_keys is not None:
            details["keys"] = changed_keys
        await record_environment_audit(
            db,
            AuditActor.for_user(actor, ip_address),
            header,
            action="environment.save",
            details=details,
            version_ids=[version.id],
        )
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise EditorError(STALE_MESSAGE) from error
    await announce_event(notifier, file_changed(header, version.number, FileChange.SAVED, actor))
    return version
