"""Environment structure: validation of the add environment form and superadmin-only creation.

An environment is created together with its first version. The target and dist paths are
resolved against the real deployment root and the allowed paths when the form is submitted, and
the importable target file and the copied dist file are read through the confined read shared
with the dist check.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    Deployment,
    Environment,
    EnvironmentVersion,
    FileType,
    Service,
    User,
)
from app.services.audit import AuditActor, EnvironmentRef, record_environment_audit
from app.services.confined_read import (
    ConfinedReadError,
    PathProblem,
    read_confined_text,
    resolve_below_root,
    split_relative,
)
from app.services.content import MAX_CONTENT_BYTES, content_problem, normalize_newlines
from app.services.deployments import FILE_MODE_PATTERN, parse_account
from app.services.file_types import (
    CERT_HAS_NO_DIST_MESSAGE,
    CHOOSE_TYPE_MESSAGE,
    content_problem_for,
    parse_file_type,
)
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    FileChange,
    FileChanged,
    announce_event,
)
from app.services.paths import AllowedPaths
from app.services.services import ServiceNameError, add_service, validate_service_name
from app.services.versions import create_version

ENVIRONMENT_NAME_PATTERN = re.compile(r"\.?[A-Za-z0-9_-][A-Za-z0-9._-]*")
MAX_ENVIRONMENT_NAME_LENGTH = 100
NEW_SERVICE = "new"
OUTSIDE_MESSAGE = "Resolves outside the deployment root and the allowed paths."


class ContentSource(StrEnum):
    """Where the first version of a new environment comes from."""

    PASTE = "paste"
    UPLOAD = "upload"
    IMPORT = "import"
    DIST = "dist"


@dataclass(frozen=True)
class EnvironmentForm:
    """The submitted add environment values, as typed.

    ``service`` is the id of an existing service of the deployment or ``NEW_SERVICE``; a typed
    ``new_service`` name always wins and creates a service. ``file_type`` is ``env`` or ``cert``,
    empty meaning ``env``. ``upload`` holds the bytes of the uploaded file, None when no file
    was chosen.
    """

    service: str = ""
    new_service: str = ""
    name: str = ""
    file_type: str = ""
    target_path: str = ""
    dist_path: str = ""
    file_mode: str = ""
    file_owner: str = ""
    file_group: str = ""
    source: str = ContentSource.PASTE.value
    content: str = ""
    upload: bytes | None = None


class EnvironmentFormError(Exception):
    """The submitted values were refused; ``errors`` maps a field name to its message."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the field messages."""
        super().__init__("; ".join(errors.values()))
        self.errors = errors


@dataclass(frozen=True)
class CleanEnvironment:
    """Validated values ready to store. ``service_id`` is None when a service is created."""

    service_id: int | None
    new_service_name: str | None
    name: str
    target_path: str
    dist_path: str | None
    file_mode: str | None
    file_owner: str | None
    file_group: str | None
    source: ContentSource
    content: str
    file_type: FileType = FileType.ENV


def relative_path(
    allowed: AllowedPaths, root: str, raw: str, label: str
) -> tuple[str | None, Path | None, str | None]:
    """The normalized relative path, its real path and None, or None, None and the message.


    The path is resolved against the real deployment root now; it must stay inside the root and
    the allowed paths, and must not be a directory. The file itself may not exist yet.
    """
    try:
        relative = split_relative(raw, label)
    except ConfinedReadError:
        text = raw.strip()
        if ".." in PurePosixPath(text).parts:
            return None, None, OUTSIDE_MESSAGE
        return None, None, "Use a path relative to the deployment root."
    try:
        _, real_path = resolve_below_root(allowed, root, relative, label)
    except ConfinedReadError as error:
        if error.problem is PathProblem.ROOT_OUTSIDE:
            return None, None, "The deployment root is outside the allowed paths."
        return None, None, OUTSIDE_MESSAGE
    if real_path.is_dir():
        return None, None, f"{label} path is a directory, not a file."
    return str(relative), real_path, None


@dataclass(frozen=True)
class TargetHint:
    """The line under the target path field: ``ok`` is False when the path cannot be used."""

    text: str
    ok: bool


def target_hint(allowed: AllowedPaths, root: str, raw: str) -> TargetHint:
    """Where a typed target path resolves, or the reason it cannot be used."""
    if not raw.strip():
        return TargetHint(f"Resolves to {root.rstrip('/')}/", True)
    _, real_path, problem = relative_path(allowed, root, raw, "Target")
    if problem or real_path is None:
        return TargetHint(problem or OUTSIDE_MESSAGE, False)
    return TargetHint(f"Resolves to {real_path}", True)


def _uploaded_text(upload: bytes | None) -> tuple[str, str | None]:
    """The text of an uploaded file and None, or an empty text and the message to show."""
    if not upload:
        return "", "Choose a text file to upload."
    if len(upload) > MAX_CONTENT_BYTES:
        return "", f"The file is larger than {MAX_CONTENT_BYTES // 1024} KiB."
    try:
        text = upload.decode("utf-8")
    except UnicodeDecodeError:
        return "", "The file is not valid UTF-8 text."
    return text, content_problem(text)


def imported_text(
    allowed: AllowedPaths, root: str, relative: str, label: str, verb: str
) -> tuple[str, str | None]:
    """The text of the file below the root and None, or an empty text and the message to show.

    ``label`` is ``target`` or ``dist`` (it names the file in the messages) and ``verb`` is what
    the form does with it (``import`` or ``copy``).
    """
    try:
        text = read_confined_text(allowed, root, relative, label=label, max_bytes=MAX_CONTENT_BYTES)
    except ConfinedReadError as error:
        messages = {
            PathProblem.NOT_A_FILE: f"The {label} file does not exist or is not a regular file.",
            PathProblem.TOO_LARGE: f"The {label} file is too large to {verb}.",
            PathProblem.NOT_UTF8: f"The {label} file is not valid UTF-8 text.",
            PathProblem.LEAVES_ROOT: OUTSIDE_MESSAGE,
        }
        return "", messages.get(error.problem, f"The {label} file cannot be read.")
    return text, content_problem(text)


async def target_in_use(
    db: AsyncSession, deployment_id: int, target_path: str, *, ignore_id: int | None = None
) -> bool:
    """Whether an environment of the deployment already has this normalized target path.

    ``ignore_id`` leaves one environment out, the one being edited.
    """
    stmt = (
        select(Environment.target_path)
        .join(Service, Service.id == Environment.service_id)
        .where(Service.deployment_id == deployment_id)
    )
    if ignore_id is not None:
        stmt = stmt.where(Environment.id != ignore_id)
    rows = await db.execute(stmt)
    return any(str(PurePosixPath(existing)) == target_path for existing in rows.scalars())


async def validate_environment(
    db: AsyncSession,
    allowed: AllowedPaths,
    deployment: Deployment,
    form: EnvironmentForm,
) -> CleanEnvironment:
    """Check every submitted value and return the clean ones, or raise with all the problems.

    ``allowed`` must be freshly loaded by the caller.
    """
    errors: dict[str, str] = {}

    service_id: int | None = None
    new_service_name: str | None = None
    if form.new_service.strip() or form.service == NEW_SERVICE:
        try:
            new_service_name = await validate_service_name(db, deployment.id, form.new_service)
        except ServiceNameError as error:
            errors["new_service"] = str(error)
    else:
        chosen = int(form.service) if form.service.isdecimal() and len(form.service) < 15 else 0
        found = (
            await db.execute(
                select(Service.id).where(
                    Service.id == chosen,
                    Service.deployment_id == deployment.id,
                )
            )
        ).scalar_one_or_none()
        if found is None:
            errors["service"] = "Choose a service."
        service_id = found

    name = form.name.strip()
    if not name:
        errors["name"] = "Enter a file name."
    elif (
        len(name) > MAX_ENVIRONMENT_NAME_LENGTH or ENVIRONMENT_NAME_PATTERN.fullmatch(name) is None
    ):
        errors["name"] = (
            "Use 1 to 100 letters, numbers, dots, dashes and underscores, "
            "starting with a letter, number or dot."
        )
    elif service_id is not None:
        taken = select(Environment.id).where(
            Environment.service_id == service_id, Environment.name == name
        )
        if (await db.execute(taken)).first() is not None:
            errors["name"] = f"This service already has a file named {name}."

    file_type = parse_file_type(form.file_type, FileType.ENV)
    if file_type is None:
        file_type = FileType.ENV
        errors["file_type"] = CHOOSE_TYPE_MESSAGE

    target_path: str | None = None
    if not form.target_path.strip():
        errors["target_path"] = "Enter a target path."
    else:
        target_path, _, problem = relative_path(
            allowed, deployment.root_path, form.target_path, "Target"
        )
        if problem:
            errors["target_path"] = problem
        elif target_path and await target_in_use(db, deployment.id, target_path):
            target_path = None
            errors["target_path"] = "Another file of this deployment already uses this target path."

    dist_path: str | None = None
    if form.dist_path.strip() and file_type is FileType.CERT:
        errors["dist_path"] = CERT_HAS_NO_DIST_MESSAGE
    elif form.dist_path.strip():
        dist_path, _, problem = relative_path(allowed, deployment.root_path, form.dist_path, "Dist")
        if problem:
            errors["dist_path"] = problem
        elif dist_path is not None and dist_path == target_path:
            dist_path = None
            errors["dist_path"] = "The dist file must be a different file from the target."

    mode = form.file_mode.strip()
    if not mode:
        file_mode = None
    elif FILE_MODE_PATTERN.fullmatch(mode) is None:
        file_mode = None
        errors["file_mode"] = "Use an octal mode such as 0600."
    else:
        file_mode = mode.zfill(4)
    owner = parse_account(form.file_owner, "Owner", errors, "file_owner")
    group = parse_account(form.file_group, "Group", errors, "file_group")

    try:
        source = ContentSource(form.source)
    except ValueError:
        source = ContentSource.PASTE
        errors["source"] = "Choose where the first version comes from."
    content = ""
    if source is ContentSource.PASTE:
        content = normalize_newlines(form.content)
        problem = content_problem(content)
        if problem:
            errors["content"] = problem
    elif source is ContentSource.UPLOAD:
        content, problem = _uploaded_text(form.upload)
        if problem:
            errors["upload"] = problem
    elif source is ContentSource.DIST:
        if file_type is FileType.CERT:
            errors["source"] = CERT_HAS_NO_DIST_MESSAGE
        elif dist_path is not None:
            content, problem = imported_text(
                allowed, deployment.root_path, dist_path, "dist", "copy"
            )
            if problem:
                errors["source"] = problem
        elif "dist_path" in errors:
            errors["source"] = "Fix the dist file path to copy the file."
        else:
            errors["source"] = "Enter the dist file path to copy the file."
    elif target_path is not None:
        content, problem = imported_text(
            allowed, deployment.root_path, target_path, "target", "import"
        )
        if problem:
            errors["source"] = problem
    elif "target_path" in errors:
        errors["source"] = "Fix the target path to import the file."

    content_field = {
        ContentSource.PASTE: "content",
        ContentSource.UPLOAD: "upload",
    }.get(source, "source")
    if content_field not in errors and (problem := content_problem_for(file_type, content)):
        errors[content_field] = problem

    if errors or target_path is None:
        raise EnvironmentFormError(errors)
    return CleanEnvironment(
        service_id=service_id,
        new_service_name=new_service_name,
        name=name,
        target_path=target_path,
        dist_path=dist_path,
        file_mode=file_mode,
        file_owner=owner,
        file_group=group,
        source=source,
        content=content,
        file_type=file_type,
    )


async def add_environment(
    db: AsyncSession,
    crypto: CryptoService,
    deployment: Deployment,
    clean: CleanEnvironment,
    *,
    actor: User,
    ip_address: str | None = None,
    comment: str | None = None,
) -> tuple[Environment, EnvironmentVersion]:
    """Add the environment, its optional new service and version 1 to the session, uncommitted.

    The caller commits and announces. ``comment`` replaces the automatic comment of version 1.
    Audit entries carry paths, attributes and the content source, never the content.
    """
    service_id = clean.service_id
    service_created = clean.new_service_name is not None
    if clean.new_service_name is not None:
        service = await add_service(
            db, deployment, clean.new_service_name, actor=actor, ip_address=ip_address
        )
        service_id = service.id
    if service_id is None:
        raise EnvironmentFormError({"service": "Choose a service."})
    environment = Environment(
        service_id=service_id,
        name=clean.name,
        file_type=clean.file_type,
        target_path=clean.target_path,
        dist_path=clean.dist_path,
        file_mode=clean.file_mode,
        file_owner=clean.file_owner,
        file_group=clean.file_group,
    )
    db.add(environment)
    await db.flush()
    version = await create_version(
        db,
        crypto,
        environment.id,
        clean.content,
        author=actor,
        comment=comment
        or (
            "Initial content (copied from the dist file)"
            if clean.source is ContentSource.DIST
            else f"Initial content ({clean.source.value})"
        ),
    )
    await record_environment_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        EnvironmentRef(
            environment.id, environment.name, deployment.project_id, deployment.id, service_id
        ),
        action="environment.create",
        details={
            "file_type": clean.file_type.value,
            "target_path": clean.target_path,
            "dist_path": clean.dist_path,
            "file_mode": clean.file_mode,
            "file_owner": clean.file_owner,
            "file_group": clean.file_group,
            "source": clean.source.value,
            "service_created": service_created,
        },
        version_ids=[version.id],
    )
    return environment, version


async def announce_created(
    notifier: EventNotifier,
    deployment: Deployment,
    environment: Environment,
    version: EnvironmentVersion,
    actor: User,
) -> None:
    """Tell the notifier that the environment was created with this first version."""
    await announce_event(
        notifier,
        FileChanged(
            deployment.project_id,
            deployment.id,
            environment.service_id,
            environment.id,
            environment.name,
            version.number,
            FileChange.CREATED,
            actor.name,
        ),
    )


async def create_environment(
    db: AsyncSession,
    crypto: CryptoService,
    deployment: Deployment,
    clean: CleanEnvironment,
    *,
    actor: User,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> Environment:
    """Create the environment, its optional new service and version 1 in one transaction.

    Audit entries carry paths, attributes and the content source, never the content.
    """
    try:
        environment, version = await add_environment(
            db, crypto, deployment, clean, actor=actor, ip_address=ip_address
        )
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise EnvironmentFormError(
            {"name": f"This service already has a file named {clean.name}."}
        ) from error
    await announce_created(notifier, deployment, environment, version, actor)
    return environment
