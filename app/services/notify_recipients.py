"""Who receives a notification.

System events go to the active superadmins. An event about a project goes to the admins of that
project, and only to those whose grants reach at least one file the event is about; each of them
is told only about the files they can reach, so a notification never names an item outside a
user's access. The recipients choice of the event decides whether the superadmins are told as
well. The extra addresses of the project the superadmin has set are told of every event about
that project whatever the choice is, with every file of the event. An address is used once, even
when two roles lead to it, and then it is told about the widest set of files any of them has.
"""

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, User, UserGroup
from app.services.mailer import valid_address
from app.services.notify_catalog import Recipients
from app.services.permissions import ItemLevel, ItemRef, load_user_scope


@dataclass(frozen=True)
class Recipient:
    """One address to write to. ``environment_ids`` limits the files the message may name to
    those ids; None means the recipient may be told about every file."""

    address: str
    environment_ids: frozenset[int] | None = None


@dataclass(frozen=True)
class ProjectItem:
    """What an event about a project is about: the service and the files it touched."""

    project_id: int
    deployment_id: int
    service_id: int
    environment_ids: tuple[int, ...]


def _usable(user: User) -> bool:
    """Whether the account is active and has an address mail can go to."""
    return user.is_active and valid_address(user.email.strip())


async def superadmin_recipients(db: AsyncSession) -> list[Recipient]:
    """The active superadmins with an address, who may be told about everything."""
    users = (
        (await db.execute(select(User).where(User.group == UserGroup.SUPERADMIN).order_by(User.id)))
        .scalars()
        .all()
    )
    return [Recipient(user.email.strip()) for user in users if _usable(user)]


async def project_admin_recipients(db: AsyncSession, item: ProjectItem) -> list[Recipient]:
    """The active admins whose grants reach the item, each with the files they may be told of.

    An event that names no file reaches an admin who covers the service itself.
    """
    admins = (
        (await db.execute(select(User).where(User.group == UserGroup.ADMIN).order_by(User.id)))
        .scalars()
        .all()
    )
    service = ItemRef(ItemLevel.SERVICE, item.project_id, item.deployment_id, item.service_id)
    found: list[Recipient] = []
    for user in admins:
        if not _usable(user):
            continue
        scope = await load_user_scope(db, user)
        if not item.environment_ids:
            if scope.covers(service):
                found.append(Recipient(user.email.strip(), frozenset()))
            continue
        visible = frozenset(
            environment_id
            for environment_id in item.environment_ids
            if scope.covers(
                ItemRef(
                    ItemLevel.ENVIRONMENT,
                    item.project_id,
                    item.deployment_id,
                    item.service_id,
                    environment_id,
                )
            )
        )
        if visible:
            found.append(Recipient(user.email.strip(), visible))
    return found


def _wider(first: frozenset[int] | None, second: frozenset[int] | None) -> frozenset[int] | None:
    """The files two roles of one address may be told of together: every file when either may."""
    if first is None or second is None:
        return None
    return first | second


def unique_by_address(recipients: Collection[Recipient]) -> list[Recipient]:
    """The recipients with each address once, ignoring case.

    The first spelling and the first position are kept; an address that leads from two roles may
    be told about the widest set of files either role has.
    """
    unique: dict[str, Recipient] = {}
    for recipient in recipients:
        key = recipient.address.lower()
        found = unique.get(key)
        if found is None:
            unique[key] = recipient
        elif _wider(found.environment_ids, recipient.environment_ids) != found.environment_ids:
            unique[key] = Recipient(
                found.address, _wider(found.environment_ids, recipient.environment_ids)
            )
    return list(unique.values())


async def extra_address_recipients(db: AsyncSession, project_id: int) -> list[Recipient]:
    """The extra addresses set on the project, each told about every file of the event.

    An address that is no longer a plain address (the stored list was changed by hand) is left
    out.
    """
    stored = await db.scalar(select(Project.extra_addresses).where(Project.id == project_id))
    return [Recipient(address) for address in stored or [] if valid_address(address)]


async def resolve_recipients(
    db: AsyncSession, choice: Recipients, item: ProjectItem | None
) -> list[Recipient]:
    """The recipients of an event for the chosen audience.

    Events about the whole system pass no item and are told to the superadmins whatever the
    choice is. For an event about a project the choice picks the superadmins, the project
    admins or both, and the extra addresses of the project are always added.
    """
    if item is None:
        return await superadmin_recipients(db)
    found: list[Recipient] = []
    if choice in (Recipients.SUPERADMINS, Recipients.BOTH):
        found += await superadmin_recipients(db)
    if choice in (Recipients.PROJECT_ADMINS, Recipients.BOTH):
        found += await project_admin_recipients(db, item)
    found += await extra_address_recipients(db, item.project_id)
    return unique_by_address(found)
