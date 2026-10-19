"""The extra notification addresses of a project: checking the typed list and saving it.

Besides the admins, a project can name addresses that are told about its deploys and files (a
team mailbox, a pager gateway). The superadmin types them one per line on the project settings
tab. Each is checked as one plain address, an address typed twice is kept once whatever its case,
and the list is bounded. The change is audited with what was added and removed; the entry has no
item scope, so only the superadmin sees the addresses.
"""

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditActorType, AuditResult, Project, User
from app.services.audit import record_audit
from app.services.mailer import valid_address

MAX_ADDRESSES = 20
MAX_TEXT_LENGTH = 8000
MAX_SHOWN_LENGTH = 60
AUDIT_ACTION = "project.notification_addresses"
_SEPARATORS = re.compile(r"[\s,;]+")


class AddressesError(Exception):
    """The typed list cannot be saved; the message says why, in words fit to show."""


@dataclass(frozen=True)
class AddressesChange:
    """What a save did: the stored list and whether it differs from the one before."""

    addresses: list[str]
    changed: bool


def parse_addresses(text: str) -> list[str]:
    """The addresses in the text, one per line (commas and semicolons separate too).

    Blank lines are ignored and an address that appears twice, whatever its case, is kept once
    with the spelling and place of its first appearance. Raises ``AddressesError`` for text that
    is too long, an address that is not a plain address, or more than ``MAX_ADDRESSES``.
    """
    if len(text) > MAX_TEXT_LENGTH:
        raise AddressesError("The list is too long.")
    found: dict[str, str] = {}
    for item in _SEPARATORS.split(text):
        if not item:
            continue
        if not valid_address(item):
            shown = item if len(item) <= MAX_SHOWN_LENGTH else f"{item[:MAX_SHOWN_LENGTH]}..."
            raise AddressesError(f"{shown} is not a valid address.")
        found.setdefault(item.lower(), item)
    if len(found) > MAX_ADDRESSES:
        raise AddressesError(f"Use at most {MAX_ADDRESSES} addresses.")
    return list(found.values())


async def save_extra_addresses(
    db: AsyncSession, project: Project, text: str, *, actor: User, ip_address: str | None = None
) -> AddressesChange:
    """Store the typed list on the project, audit what changed and commit.

    Nothing is written when the list is the one already stored. Raises ``AddressesError``.
    """
    addresses = parse_addresses(text)
    previous = list(project.extra_addresses or [])
    if addresses == previous:
        return AddressesChange(addresses, False)
    kept = {address.lower() for address in addresses}
    before = {address.lower() for address in previous}
    project.extra_addresses = addresses
    await record_audit(
        db,
        action=AUDIT_ACTION,
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=actor,
        ip_address=ip_address,
        target_type="project",
        target_id=project.id,
        target_name=project.name,
        details={
            "added": [address for address in addresses if address.lower() not in before],
            "removed": [address for address in previous if address.lower() not in kept],
            "count": len(addresses),
        },
    )
    await db.commit()
    return AddressesChange(addresses, True)
