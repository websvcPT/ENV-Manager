"""The Compose files linked to a deployment, read only.

A deployment keeps the Compose files it runs from in ``Deployment.compose_files`` (paths relative
to the root, base file first), set by the Compose import or linked later by the superadmin
without importing again. The app never manages, versions or writes them: they usually live in
the project's own repository. It shows their current text, read from the host when asked, to
those who may (``Capability.VIEW_COMPOSE_FILES``), and audits every view.

A Compose file the deployment files hold as a managed file is removed from them when it is
linked, with its versions; the file on the host is not touched. A startup step does the same for
the deployments whose files were imported before this rule. A file an API token is restricted to
is kept, and the link or the startup step says so.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import get_logger
from app.models import (
    DEPLOYMENT_FILES_NAME,
    AuditResult,
    Deployment,
    Environment,
    EnvironmentVersion,
    Service,
    User,
)
from app.services.audit import AuditActor, record_actor_audit, record_environment_audit
from app.services.compose_config import ComposeScanError
from app.services.compose_scan import resolve_compose_files
from app.services.paths import AllowedPaths, load_allowed_paths
from app.services.services import find_deployment_files
from app.services.token_guard import restrictions_below
from app.services.tokens import is_live

log = get_logger("compose_files")

ACTION_VIEW = "deployment.compose_view"
ACTION_LINK = "deployment.compose_link"
UPGRADE_ACTOR = "upgrade"
REMOVED_REASON = "compose_file_linked"
NOT_LINKED_MESSAGE = "This Compose file is not linked to the deployment."
NOT_TEXT_MESSAGE = "The Compose file is not UTF-8 text."


@dataclass(frozen=True)
class ComposeText:
    """The current text of one linked Compose file, as read from the host."""

    path: str
    text: str


@dataclass(frozen=True)
class DroppedFiles:
    """The target paths of the deployment files removed because they are linked Compose files,
    and of those kept because an API token is restricted to them."""

    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FileRef:
    """A deployment file with the ids above it, as its audit entry needs them."""

    id: int
    name: str
    project_id: int
    deployment_id: int
    service_id: int


async def drop_managed_compose_files(
    db: AsyncSession, deployment: Deployment, who: AuditActor
) -> DroppedFiles:
    """Remove from the deployment files every file whose target is a linked Compose file.

    Each removal is audited as ``environment.delete`` and takes the file's versions with it; the
    file on the host is not touched. A file an API token that can still be used is restricted
    to is kept; the restrictions of dead tokens go with the file. Nothing is committed.
    """
    linked = {str(PurePosixPath(path)) for path in deployment.compose_files or ()}
    files = await find_deployment_files(db, deployment.id)
    if not linked or files is None:
        return DroppedFiles()
    environments = await db.scalars(
        select(Environment).where(Environment.service_id == files.id).order_by(Environment.id)
    )
    removed: list[str] = []
    kept: list[str] = []
    for environment in environments:
        if str(PurePosixPath(environment.target_path)) not in linked:
            continue
        restricting = await restrictions_below(db, environment_id=environment.id)
        if any(is_live(token) for token, _ in restricting):
            kept.append(environment.target_path)
            continue
        for _, restriction in restricting:
            await db.delete(restriction)
        versions = (
            await db.execute(
                select(func.count())
                .select_from(EnvironmentVersion)
                .where(EnvironmentVersion.environment_id == environment.id)
            )
        ).scalar_one()
        reference = _FileRef(
            environment.id, environment.name, deployment.project_id, deployment.id, files.id
        )
        await record_environment_audit(
            db,
            who,
            reference,
            action="environment.delete",
            details={
                "versions": versions,
                "target_path": environment.target_path,
                "reason": REMOVED_REASON,
            },
        )
        await db.delete(environment)
        removed.append(environment.target_path)
    await db.flush()
    return DroppedFiles(tuple(removed), tuple(kept))


async def drop_managed_compose_files_everywhere(db: AsyncSession) -> DroppedFiles:
    """The startup step: remove the linked Compose files from the deployment files of every
    deployment, as the system, and commit. Idempotent; a kept file is logged by deployment id."""
    deployments = await db.scalars(
        select(Deployment)
        .join(Service, Service.deployment_id == Deployment.id)
        .where(Deployment.compose_files.is_not(None), Service.name == DEPLOYMENT_FILES_NAME)
        .order_by(Deployment.id)
    )
    who = AuditActor.system(UPGRADE_ACTOR)
    removed: list[str] = []
    kept: list[str] = []
    for deployment in deployments.all():
        dropped = await drop_managed_compose_files(db, deployment, who)
        removed.extend(dropped.removed)
        kept.extend(dropped.kept)
        if dropped.kept:
            log.warning(
                "deployment %s keeps %d Compose file(s) in its deployment files: "
                "an API token is restricted to them",
                deployment.id,
                len(dropped.kept),
            )
    await db.commit()
    return DroppedFiles(tuple(removed), tuple(kept))


async def link_compose_files(
    db: AsyncSession,
    deployment: Deployment,
    raw_paths: Sequence[str],
    *,
    actor: User,
    ip_address: str | None = None,
) -> DroppedFiles:
    """Link the Compose files of a deployment (base first), or unlink them with an empty list.

    Every path is checked as the import checks it: relative to the root, inside it and the
    allowed paths, a regular file within the size limit. Raises ``ComposeScanError`` with a
    message safe to show, changing nothing. The linked files the deployment files hold are
    removed from them; the change is audited and committed.
    """
    relative: list[str] = []
    if raw_paths:
        allowed = await load_allowed_paths(db)
        resolved = await asyncio.to_thread(
            resolve_compose_files, allowed, deployment.root_path, list(raw_paths)
        )
        relative = list(resolved.relative)
    previous = list(deployment.compose_files or ())
    deployment.compose_files = relative or None
    who = AuditActor.for_user(actor, ip_address)
    dropped = await drop_managed_compose_files(db, deployment, who)
    await record_actor_audit(
        db,
        who,
        action=ACTION_LINK,
        result=AuditResult.SUCCESS,
        target_type="deployment",
        target_id=deployment.id,
        target_name=deployment.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        details={
            "compose_files": relative,
            "previous": previous,
            "removed_files": list(dropped.removed),
            "kept_files": list(dropped.kept),
        },
    )
    await db.commit()
    return dropped


def _read_text(allowed: AllowedPaths, root_path: str, path: str) -> str:
    """The text of one Compose file, checked and read now. Blocking."""
    resolved = resolve_compose_files(allowed, root_path, [path])
    try:
        return resolved.paths[0].read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise ComposeScanError(NOT_TEXT_MESSAGE) from error
    except OSError as error:
        raise ComposeScanError("The Compose file cannot be read.") from error


async def read_compose_file(
    db: AsyncSession,
    deployment: Deployment,
    index: int,
    *,
    actor: User,
    ip_address: str | None = None,
) -> ComposeText:
    """The current text of the linked Compose file at ``index``, read from the host, and the
    audited view. Raises ``ComposeScanError`` with a message safe to show when the file is not
    linked or cannot be read now; nothing is audited then."""
    linked = deployment.compose_files or []
    if not 0 <= index < len(linked):
        raise ComposeScanError(NOT_LINKED_MESSAGE)
    path = linked[index]
    allowed = await load_allowed_paths(db)
    text = await asyncio.to_thread(_read_text, allowed, deployment.root_path, path)
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action=ACTION_VIEW,
        result=AuditResult.SUCCESS,
        target_type="deployment",
        target_id=deployment.id,
        target_name=deployment.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        details={"file": path},
    )
    await db.commit()
    return ComposeText(path, text)
