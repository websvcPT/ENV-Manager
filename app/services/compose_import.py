"""Importing the services and files found by a Compose scan into a deployment that has none.

The import repeats the scan when it runs, so what it creates is what the files hold now and the
choices of the user (service names and file paths) are matched against the new scan, never taken
as given. Everything happens in one transaction, all or nothing: the services, one environment
per chosen file with its first version, the Compose files stored on the deployment and the audit
entries. A file that exists becomes version 1 with its exact content, as the drift view's Import
saves a target; a file that does not exist becomes version 1 with the content of its dist file
when the user starts it from that file, else an empty version 1, and is never written by the
import. Each service that has files found on the host gets one run with the "Imported from
Compose" trigger and an unchanged result per such file, which records version 1 as the content
the target held, so the drift check has a baseline from the start and a version 2 saved before
the first deploy does not read as drift. Files that were not on the host get no result: their
first deploy writes them.

The deployment's own files (the deployment ``.env`` and the Compose files) go to its deployment
files, created when one of them is chosen. A post-deploy command is stored only for a chosen
service, or for the deployment files when one of their files is chosen, only while the Docker
socket is usable, and only after the safety net accepts every line; each stored command has a
``service.hook_save`` entry like a save from the service page, which is the only place its text
is recorded.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    DEPLOYMENT_FILES_NAME,
    AuditResult,
    Deployment,
    Environment,
    EnvironmentVersion,
    FileType,
    HookType,
    Service,
    ServiceHook,
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
from app.services.deploy_records import record_import_run
from app.services.docker_cli import DockerCli
from app.services.environments import (
    ContentSource,
    EnvironmentForm,
    EnvironmentFormError,
    add_environment,
    announce_created,
    validate_environment,
)
from app.services.hook_commands import CommandError, normalize_commands, parse_commands
from app.services.hook_settings import ACTION_SAVE, DEFAULT_TIMEOUT_SECONDS, UNAVAILABLE_SUFFIX
from app.services.notify_events import NO_EVENT_NOTIFIER, EventNotifier
from app.services.paths import load_allowed_paths
from app.services.services import (
    ServiceNameError,
    add_service,
    ensure_deployment_files,
    listed_service,
    validate_service_name,
)

HAS_SERVICES_MESSAGE = "This deployment already has services, so nothing can be imported."
CONFLICT_MESSAGE = "The deployment changed while importing. Nothing was imported."
NO_SERVICE_MESSAGE = "Select at least one service."
UNKNOWN_SERVICE_MESSAGE = "This service is not in the Compose files or cannot be used."
NOT_SELECTED_REASON = "Not selected."
IMPORT_COMMENT = "Imported from Docker Compose"
EMPTY_COMMENT = "Created empty from Docker Compose"
DIST_COMMENT = "Created from the dist file by the Docker Compose import"
EXISTS_FROM_DIST_MESSAGE = "The file is on disk, so it is imported as it is."
DIST_NEEDED_MESSAGE = "Starting from the dist file needs the dist file linked."
COMMAND_OWNER_MESSAGE = "A post-deploy command needs its service or a deployment file selected."
COMMAND_STORED_MESSAGE = "A post-deploy command is already set here."
EMPTY_COMMAND_MESSAGE = "Enter the command, or untick it."
MAX_AUDITED_SKIPPED = 100
ACTION = "deployment.import_compose"
COMMAND_PREFIX = "command:"


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
    None. ``from_dist`` starts a file that is not on disk from its linked dist file.
    """

    key: str
    owner: str
    file_type: FileType | None = None
    dist_path: str | None = None
    from_dist: bool = False


@dataclass(frozen=True)
class ComposeSelection:
    """What the user chose: the Compose files (base first), the service names and the files.

    A file of the deployment files has ``DEPLOYMENT_FILES_NAME`` as its owner. ``commands`` are
    the ticked post-deploy commands as (owner, command text) pairs, the owner being a service
    name or ``DEPLOYMENT_FILES_NAME``.
    """

    compose_files: tuple[str, ...]
    services: tuple[str, ...]
    files: tuple[FileChoice, ...]
    commands: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ComposeImportResult:
    """What an import created: counts only. ``with_content`` files start with text (from the file
    on disk or from their dist file), ``from_dist`` of them from the dist file, ``empty`` ones
    start empty. ``environments`` counts the deployment files too, ``deployment_files`` only
    them, and ``commands`` the post-deploy commands stored."""

    services: int
    environments: int
    with_content: int
    empty: int
    skipped: int
    from_dist: int = 0
    deployment_files: int = 0
    commands: int = 0


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
    by_key = {candidate.key: candidate for candidate in scan.all_files}
    own = {candidate.key for candidate in scan.deployment_files}
    seen: set[str] = set()
    picked: list[tuple[FileCandidate, FileChoice, FileType]] = []
    for choice in selection.files:
        candidate = by_key.get(choice.key)
        if candidate is None or choice.key in seen:
            errors[choice.key] = "This file is not offered or is chosen twice."
            continue
        seen.add(choice.key)
        file_type = choice.file_type or candidate.file_type
        owners = [DEPLOYMENT_FILES_NAME] if choice.key in own else chosen
        problem = (
            candidate.disabled_reason
            or candidate.type_problems.get(file_type.value)
            or _choice_problem(candidate, choice, file_type, owners)
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


def plan_commands(
    selection: ComposeSelection, services: Sequence[str], has_deployment_files: bool
) -> list[tuple[str, str]]:
    """The ticked post-deploy commands in their stored form, as (owner, commands) pairs.

    An owner must be one of the chosen ``services``, or the deployment files when one of their
    files is chosen, and every line must pass the safety net; otherwise ``ComposeImportError``
    names each refused command by ``command:<owner>``.
    """
    errors: dict[str, str] = {}
    planned: list[tuple[str, str]] = []
    for owner, text in dict(selection.commands).items():
        key = f"{COMMAND_PREFIX}{owner}"
        own = owner == DEPLOYMENT_FILES_NAME
        if not (has_deployment_files if own else owner in services):
            errors[key] = COMMAND_OWNER_MESSAGE
            continue
        try:
            parse_commands(text)
        except CommandError as error:
            errors[key] = str(error)
            continue
        commands = normalize_commands(text)
        if not commands:
            errors[key] = EMPTY_COMMAND_MESSAGE
            continue
        planned.append((owner, commands))
    if errors:
        raise ComposeImportError("Some of the post-deploy commands cannot be set.", errors)
    return planned


def _choice_problem(
    candidate: FileCandidate, choice: FileChoice, file_type: FileType, services: Sequence[str]
) -> str | None:
    """Why the owner or the dist file of a choice is refused, None when both are fine.
    ``services`` are the owners the file may have."""
    if choice.owner not in services:
        return "Choose one of the selected services as the owner."
    if choice.dist_path is not None:
        if file_type is FileType.CERT:
            return "A certificate has no dist file."
        if choice.dist_path != candidate.dist_suggestion:
            return "The dist file is not one of the suggested files."
    if choice.from_dist:
        if candidate.exists:
            return EXISTS_FROM_DIST_MESSAGE
        if choice.dist_path is None:
            return DIST_NEEDED_MESSAGE
    return None


def _source_of(entry: _Entry) -> ContentSource:
    """Where version 1 comes from: the file on disk, the dist file, or nothing (empty)."""
    if entry.candidate.exists:
        return ContentSource.IMPORT
    return ContentSource.DIST if entry.choice.from_dist else ContentSource.PASTE


def _comment_of(entry: _Entry) -> str:
    """The comment of version 1, saying where its content came from."""
    if entry.candidate.exists:
        return IMPORT_COMMENT
    return DIST_COMMENT if entry.choice.from_dist else EMPTY_COMMENT


def _form_for(entry: _Entry, service_id: int) -> EnvironmentForm:
    """The add environment form of one file: a file on the host is imported, a missing one starts
    from its dist file when chosen, else empty."""
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
        source=_source_of(entry).value,
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
    own = {candidate.key for candidate in scan.deployment_files}
    for candidate in scan.all_files:
        if candidate.key not in chosen_files:
            skipped.append(
                {
                    "kind": "deployment_file" if candidate.key in own else "file",
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
    a choice does not match, a command is refused or Docker is not usable for it, or a file
    cannot be created: the error then lists each file and command. Deployment files that exist
    already are kept and receive the chosen files.
    """
    count = (
        select(func.count())
        .select_from(Service)
        .where(Service.deployment_id == deployment.id, listed_service())
    )
    if (await db.execute(count)).scalar_one():
        raise ComposeImportError(HAS_SERVICES_MESSAGE)
    try:
        scan = await scan_compose(db, cli, deployment, selection.compose_files)
    except ComposeScanError as error:
        raise ComposeImportError(str(error)) from error
    entries = _plan(scan, selection)
    own = [entry for entry in entries if entry.choice.owner == DEPLOYMENT_FILES_NAME]
    chosen = list(dict.fromkeys(selection.services))
    commands = plan_commands(selection, chosen, bool(own))
    if commands:
        docker = await cli.status(fresh=True)
        if not docker.available:
            raise ComposeImportError(f"{docker.reason} {UNAVAILABLE_SUFFIX}")
    allowed = await load_allowed_paths(db)
    try:
        services: dict[str, Service] = {}
        for name in chosen:
            try:
                checked = await validate_service_name(db, deployment.id, name)
            except ServiceNameError as error:
                raise ComposeImportError(
                    "A service cannot be created.", {f"service:{name}": str(error)}
                ) from error
            services[name] = await add_service(
                db, deployment, checked, actor=actor, ip_address=ip_address
            )
        owners = dict(services)
        if own:
            owners[DEPLOYMENT_FILES_NAME] = await ensure_deployment_files(
                db, deployment, actor=actor, ip_address=ip_address
            )
        created: list[tuple[Environment, EnvironmentVersion]] = []
        found_on_host: dict[str, list[tuple[Environment, EnvironmentVersion]]] = {}
        errors: dict[str, str] = {}
        for entry in entries:
            form = _form_for(entry, owners[entry.choice.owner].id)
            try:
                clean = await validate_environment(db, allowed, deployment, form)
            except EnvironmentFormError as error:
                errors[entry.candidate.key] = next(iter(error.errors.values()))
                continue
            pair = await add_environment(
                db,
                crypto,
                deployment,
                clean,
                actor=actor,
                ip_address=ip_address,
                comment=_comment_of(entry),
            )
            created.append(pair)
            if entry.candidate.exists:
                found_on_host.setdefault(entry.choice.owner, []).append(pair)
        if errors:
            raise ComposeImportError("Some of the files cannot be imported.", errors)
        who = AuditActor.for_user(actor, ip_address)
        for owner, text in commands:
            await _store_command(db, who, deployment, owners[owner], text)
        deployment.compose_files = list(scan.compose_files)
        skipped = _skipped(scan, selection, entries)
        from_dist = sum(1 for entry in entries if entry.choice.from_dist)
        with_content = sum(1 for entry in entries if entry.candidate.exists) + from_dist
        audit_entry = await record_actor_audit(
            db,
            who,
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
                "deployment_files": len(own),
                "with_content": with_content,
                "from_dist": from_dist,
                "empty": len(created) - with_content,
                "post_deploy": [owner for owner, _ in commands],
                "skipped_count": len(skipped),
                "skipped": skipped[:MAX_AUDITED_SKIPPED],
            },
        )
        for owner, files in found_on_host.items():
            await record_import_run(
                db,
                service=owners[owner],
                deployment=deployment,
                user=actor,
                audit_entry_id=audit_entry.id,
                files=files,
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
        from_dist=from_dist,
        deployment_files=len(own),
        commands=len(commands),
    )


async def _store_command(
    db: AsyncSession, who: AuditActor, deployment: Deployment, service: Service, commands: str
) -> None:
    """Add the post-deploy command of a service, with the defaults of the settings form (the
    deployment root as working directory, run only when the deploy changed a file), and the
    ``service.hook_save`` entry that keeps its text for the superadmin. Raises
    ``ComposeImportError`` when the service has a command already."""
    stored = await db.scalar(
        select(ServiceHook.id).where(
            ServiceHook.service_id == service.id, ServiceHook.type == HookType.POST_DEPLOY
        )
    )
    if stored is not None:
        raise ComposeImportError(
            "Some of the post-deploy commands cannot be set.",
            {f"{COMMAND_PREFIX}{service.name}": COMMAND_STORED_MESSAGE},
        )
    db.add(
        ServiceHook(
            service_id=service.id,
            type=HookType.POST_DEPLOY,
            commands=commands,
            working_directory=None,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            run_only_if_changed=True,
        )
    )
    await record_actor_audit(
        db,
        who,
        action=ACTION_SAVE,
        result=AuditResult.SUCCESS,
        target_type="service",
        target_id=service.id,
        target_name=service.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
        details={
            "hook": HookType.POST_DEPLOY.value,
            "changed": ["commands", "timeout", "run_only_if_changed"],
            "commands": len(parse_commands(commands)),
            "previous_commands": "",
            "new_commands": commands,
        },
    )
