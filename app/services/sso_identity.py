"""Which accounts are linked to a single sign-on identity, and what that changes for them.

An account is linked when it has an identity row, whether it was created at its first sign in
or an existing account was linked by its email address. While single sign-on is on, the provider
owns the profile, the password and the second factor of a linked account that is not a
superadmin ("managed"); a linked superadmin keeps its password and second factor here.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserGroup, UserIdentity
from app.services.app_settings import StoredSettings


@dataclass(frozen=True)
class SsoAccount:
    """How single sign-on touches one account.

    ``linked`` accounts show the link to the provider's account page (``url``); ``managed`` ones
    can not change their profile, password or second factor here.
    """

    linked: bool = False
    managed: bool = False
    url: str = ""


async def linked_user_ids(db: AsyncSession, user_ids: Iterable[int]) -> frozenset[int]:
    """The ids among ``user_ids`` of the accounts that are linked to an identity."""
    wanted = list(user_ids)
    if not wanted:
        return frozenset()
    rows = await db.execute(select(UserIdentity.user_id).where(UserIdentity.user_id.in_(wanted)))
    return frozenset(rows.scalars())


def is_managed(stored: StoredSettings, *, linked: bool, group: UserGroup) -> bool:
    """Whether the provider owns the profile, password and second factor of the account now.

    True for a linked account that is not a superadmin, while sign in through the provider is on.
    """
    return stored.sso_on and linked and group is not UserGroup.SUPERADMIN


async def sso_account(db: AsyncSession, user: User, stored: StoredSettings) -> SsoAccount:
    """The account's relation to single sign-on; nothing is read while sign in is off."""
    if not stored.sso_on or not await is_linked(db, user.id):
        return SsoAccount()
    url = stored.sso.effective_account_url
    return SsoAccount(
        linked=True,
        managed=is_managed(stored, linked=True, group=user.group),
        url=url if url.startswith(("https://", "http://")) else "",
    )


async def is_linked(db: AsyncSession, user_id: int) -> bool:
    """Whether the account is linked to a single sign-on identity."""
    return user_id in await linked_user_ids(db, [user_id])
