"""Importing the services and files found by a Compose scan into a deployment that has none.

The import repeats the scan when it runs, so what it creates is what the files hold now and the
choices of the user (service names and file paths) are matched against the new scan, never taken
as given. Everything happens in one transaction, all or nothing: the services, one environment
per chosen file with its first version, the Compose files stored on the deployment and the audit
entries. A file that exists becomes version 1 with its exact content, as the drift view's Import
saves a target; a file that does not exist becomes an empty version 1 and is never written by
the import. Version 1 equal to the file on disk is what lets the first deploy see the file in
sync, so no deploy result is recorded here.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    AuditResult,
    Deployment,
    Environment,
    EnvironmentVersion,
    FileType,
    Service,
    User,
)
from app.services.audit import AuditActor, record_actor_audit
from app.services.compose_config import ComposeScanError
from app.services.compose_scan import (
    ComposeScan,
    FileCandidate,
    assign_names,
    scan_compose,
)
from app.services.docker_cli import DockerCli
from app.services.environments import (
    ContentSource,
    EnvironmentForm,
    EnvironmentFormError,
    add_environment,
    announce_created,
    validate_environment,
)
from app.services.notify_events import NO_EVENT_NOTIFIER, EventNotifier
from app.services.paths import load_allowed_paths
from app.services.services import ServiceNameError, add_service, validate_service_name

HAS_SERVICES_MESSAGE = "This deployment already has services, so nothing can be imported."
CONFLICT_MESSAGE = "The deployment changed while importing. Nothing was imported."
NO_SERVICE_MESSAGE = "Select at least one service."
UNKNOWN_SERVICE_MESSAGE = "This service is not in the Compose files or cannot be used."
NOT_SELECTED_REASON = "Not selected."
IMPORT_COMMENT = "Imported from Docker Compose"
EMPTY_COMMENT = "Created empty from Docker Compose"
MAX_AUDITED_SKIPPED = 100
ACTION = "deployment.import_compose"


class ComposeImportError(Exception):
    """The import was refused and nothing was changed.

    ``errors`` maps what is wrong to a message: the key of a file candidate or ``service:<name>``;
    it is empty when the whole request is refused.
    """

    def __init__(self, message: str, errors: dict[str, str] | None = None) -> None:
        """Hold the message and the per-item messages."""
        super().__init__(message)
        self.errors = errors or {}


@dataclass(frozen=True)
class FileChoice:
    """A file the user ticked: its key, the service that owns it, and the optional overrides.

    ``file_type`` None keeps the suggested type; ``dist_path`` must be the suggested sibling or
    None.
    """

    key: str
    owner: str
    file_type: FileType | None = None
    dist_path: str | None = None


@dataclass(frozen=True)
class ComposeSelection:
    """What the user chose: the Compose files (base first), the service names and the files."""

    compose_files: tuple[str, ...]
    services: tuple[str, ...]
    files: tuple[FileChoice, ...]


@dataclass(frozen=True)
class ComposeImportResult:
    """What an import created: counts only. ``with_content`` files held text, ``empty`` ones did
    not exist and start empty."""

    services: int
    environments: int
    with_content: int
    empty: int
    skipped: int


@dataclass(frozen=True)
class _Entry:
    """One file to create: its candidate, the choice and the name it gets."""

    candidate: FileCandidate
    choice: FileChoice
    file_type: FileType
    name: str | None


def _plan(scan: ComposeScan, selection: ComposeSelection) -> list[_Entry]:
    """Match the choices against the fresh scan, or raise with a message for each mismatch."""
    errors: dict[str, str] = {}
    usable = {service.name for service in scan.services if service.disabled_reason is None}
    chosen = list(dict.fromkeys(selection.services))
    if not chosen:
        raise ComposeImportError(NO_SERVICE_MESSAGE)
    for name in chosen:
        if name not in usable:
            errors[f"service:{name}"] = UNKNOWN_SERVICE_MESSAGE
    by_key = {candidate.key: candidate for candidate in scan.files}
    seen: set[str] = set()
    picked: list[tuple[FileCandidate, FileChoice, FileType]] = []
    for choice in selection.files:
        candidate = by_key.get(choice.key)
        if candidate is None or choice.key in seen:
            errors[choice.key] = "This file is not offered or is chosen twice."
            continue
        seen.add(choice.key)
        file_type = choice.file_type or candidate.file_type
        problem = (
            candidate.disabled_reason
            or candidate.type_problems.get(file_type.value)
            or _choice_problem(candidate, choice, file_type, chosen)
        )
        if problem:
            errors[choice.key] = problem
            continue
        picked.append((candidate, choice, file_type))
    names = assign_names(
        (candidate.key, PurePosixPath(candidate.key).name, choice.owner)
        for candidate, choice, _ in picked
    )
    entries = [
        _Entry(candidate, choice, kind, names[candidate.key]) for candidate, choice, kind in picked
    ]
    for entry in entries:
        if entry.name is None and entry.candidate.key not in errors:
            errors[entry.candidate.key] = "The file name cannot be used."
    if errors:
        raise ComposeImportError("Some of the choices cannot be imported.", errors)
    return entries


def _choice_problem(
    candidate: FileCandidate, choice: FileChoice, file_type: FileType, services: Sequence[str]
) -> str | None:
    """Why the owner or the dist file of a choice is refused, None when both are fine."""
    if choice.owner not in services:
        return "Choose one of the selected services as the owner."
    if choice.dist_path is not None:
        if file_type is FileType.CERT:
            return "A certificate has no dist file."
        if choice.dist_path != candidate.dist_suggestion:
            return "The dist file is not one of the suggested files."
    return None


def _form_for(entry: _Entry, service_id: int) -> EnvironmentForm:
    """The add environment form of one file: a file on the host is imported, a missing one empty."""
    candidate = entry.candidate
    return EnvironmentForm(
        service=str(service_id),
        name=entry.name or "",
        file_type=entry.file_type.value,
        target_path=candidate.key,
        dist_path=entry.choice.dist_path or "",
        file_mode=candidate.file_mode or "",
        file_owner=candidate.file_owner or "",
        file_group=candidate.file_group or "",
        source=(ContentSource.IMPORT if candidate.exists else ContentSource.PASTE).value,
    )


def _skipped(
    scan: ComposeScan, selection: ComposeSelection, entries: list[_Entry]
) -> list[dict[str, str]]:
    """The candidates and ignored items that were not imported, with the reason."""
    chosen_services = set(selection.services)
    chosen_files = {entry.candidate.key for entry in entries}
    skipped: list[dict[str, str]] = []
    for service in scan.services:
        if service.disabled_reason is not None or service.name not in chosen_services:
            skipped.append(
                {
                    "kind": "service",
                    "name": service.name,
                    "reason": service.disabled_reason or NOT_SELECTED_REASON,
                }
            )
    for candidate in scan.files:
        if candidate.key not in chosen_files:
            skipped.append(
                {
                    "kind": "file",
                    "name": candidate.key,
                    "reason": candidate.disabled_reason or NOT_SELECTED_REASON,
                }
            )
    skipped.extend(
        {"kind": "ignored", "name": item.label, "reason": item.reason} for item in scan.ignored
    )
    return skipped


async def import_compose(
    db: AsyncSession,
    crypto: CryptoService,
    cli: DockerCli,
    deployment: Deployment,
    selection: ComposeSelection,
    *,
    actor: User,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> ComposeImportResult:
    """Create the chosen services and files of a deployment that has no services yet.

    Scans the Compose files again and matches the choices against the result. Refused with
    ``ComposeImportError`` (nothing is changed) when the deployment has services, the scan fails,
    a choice does not match, or a file cannot be created: the error then lists each file.
    """
    count = select(func.count()).select_from(Service).where(Service.deployment_id == deployment.id)
    if (await db.execute(count)).scalar_one():
        raise ComposeImportError(HAS_SERVICES_MESSAGE)
    try:
        scan = await scan_compose(db, cli, deployment, selection.compose_files)
    except ComposeScanError as error:
        raise ComposeImportError(str(error)) from error
    entries = _plan(scan, selection)
    allowed = await load_allowed_paths(db)
    try:
        services: dict[str, Service] = {}
        for name in dict.fromkeys(selection.services):
            try:
                checked = await validate_service_name(db, deployment.id, name)
            except ServiceNameError as error:
                raise ComposeImportError(
                    "A service cannot be created.", {f"service:{name}": str(error)}
                ) from error
            services[name] = await add_service(
                db, deployment, checked, actor=actor, ip_address=ip_address
            )
        created: list[tuple[Environment, EnvironmentVersion]] = []
        errors: dict[str, str] = {}
        for entry in entries:
            form = _form_for(entry, services[entry.choice.owner].id)
            try:
                clean = await validate_environment(db, allowed, deployment, form)
            except EnvironmentFormError as error:
                errors[entry.candidate.key] = next(iter(error.errors.values()))
                continue
            created.append(
                await add_environment(
                    db,
                    crypto,
                    deployment,
                    clean,
                    actor=actor,
                    ip_address=ip_address,
                    comment=IMPORT_COMMENT if entry.candidate.exists else EMPTY_COMMENT,
                )
            )
        if errors:
            raise ComposeImportError("Some of the files cannot be imported.", errors)
        deployment.compose_files = list(scan.compose_files)
        skipped = _skipped(scan, selection, entries)
        with_content = sum(1 for entry in entries if entry.candidate.exists)
        await record_actor_audit(
            db,
            AuditActor.for_user(actor, ip_address),
            action=ACTION,
            result=AuditResult.SUCCESS,
            target_type="deployment",
            target_id=deployment.id,
            target_name=deployment.name,
            project_id=deployment.project_id,
            deployment_id=deployment.id,
            details={
                "compose_files": list(scan.compose_files),
                "services": list(services),
                "environments": len(created),
                "with_content": with_content,
                "empty": len(created) - with_content,
                "skipped_count": len(skipped),
                "skipped": skipped[:MAX_AUDITED_SKIPPED],
            },
        )
        await db.commit()
    except ComposeImportError:
        await db.rollback()
        raise
    except IntegrityError as error:
        await db.rollback()
        raise ComposeImportError(CONFLICT_MESSAGE) from error
    for environment, version in created:
        await announce_created(notifier, deployment, environment, version, actor)
    return ComposeImportResult(
        services=len(services),
        environments=len(created),
        with_content=with_content,
        empty=len(created) - with_content,
        skipped=len(skipped),
    )
