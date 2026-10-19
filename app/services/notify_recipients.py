"""Who receives a notification.

System events go to the active superadmins. An event about a project goes to the admins of that
project, and only to those whose grants reach at least one file the event is about; each of them
is told only about the files they can reach, so a notification never names an item outside a
user's access. The recipients choice of the event decides whether the superadmins are told as
well. An address is used once, even when two roles lead to it.
"""

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserGroup
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


def unique_by_address(recipients: Collection[Recipient]) -> list[Recipient]:
    """The recipients with each address once, the first one winning and the order kept."""
    seen: set[str] = set()
    unique: list[Recipient] = []
    for recipient in recipients:
        key = recipient.address.lower()
        if key not in seen:
            seen.add(key)
            unique.append(recipient)
    return unique


async def resolve_recipients(
    db: AsyncSession, choice: Recipients, item: ProjectItem | None
) -> list[Recipient]:
    """The recipients of an event for the chosen audience.

    Events about the whole system pass no item and are told to the superadmins whatever the
    choice is. For an event about a project the choice picks the superadmins, the project
    admins or both.
    """
    if item is None:
        return await superadmin_recipients(db)
    found: list[Recipient] = []
    if choice in (Recipients.SUPERADMINS, Recipients.BOTH):
        found += await superadmin_recipients(db)
    if choice in (Recipients.PROJECT_ADMINS, Recipients.BOTH):
        found += await project_admin_recipients(db, item)
    return unique_by_address(found)
