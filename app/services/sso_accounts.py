"""The account a single sign in is for: found by its identity or its email, linked or created,
and brought in line with the provider."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditResult, User, UserGroup, UserIdentity
from app.services.app_settings import SsoSettings
from app.services.audit import AuditActor, record_actor_audit
from app.services.sso_claims import FALLBACK_NAME, SsoClaims, group_for
from app.services.sso_refusal import (
    ALREADY_LINKED,
    CREATION_OFF,
    CREATION_UNVERIFIED,
    EMAIL_COLLISION,
    EMAIL_UNVERIFIED,
    LINK_OFF,
    NO_MATCHING_ROLE,
    USER_DISABLED,
    SsoRefusal,
)
from app.services.users import (
    UserExistsError,
    create_sso_user,
    find_user_by_email,
    set_user_group,
)

SSO_ACTOR = "sso"


@dataclass
class Attempt:
    """What is known of a sign in while it is checked, for the audit entry of a refusal."""

    user: User | None = None
    email: str | None = None


async def _find_identity(db: AsyncSession, issuer: str, subject: str) -> UserIdentity | None:
    """The link for an issuer and subject, if there is one."""
    return (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.issuer == issuer, UserIdentity.subject == subject
            )
        )
    ).scalar_one_or_none()


async def _has_identity(db: AsyncSession, user: User) -> bool:
    """Whether the account is already linked to an identity."""
    return (
        await db.execute(select(UserIdentity.id).where(UserIdentity.user_id == user.id))
    ).first() is not None


async def _account_to_sign_in(
    db: AsyncSession, settings: SsoSettings, issuer: str, claims: SsoClaims, attempt: Attempt
) -> tuple[User | None, UserIdentity | None]:
    """The account the identity signs in and the link to make, after every refusal is checked.

    The account is None when one is to be created. The link is None when the identity is
    already linked.
    """
    identity = await _find_identity(db, issuer, claims.subject)
    user: User | None = None
    if identity is not None:
        user = await db.get(User, identity.user_id)
        attempt.user = user
        holder = await find_user_by_email(db, claims.email)
        if user is not None and holder is not None and holder.id != user.id:
            raise SsoRefusal(EMAIL_COLLISION)
        link = None
    else:
        user = await find_user_by_email(db, claims.email)
        attempt.user = user
        if user is None:
            if not settings.create_users:
                raise SsoRefusal(CREATION_OFF)
            if not claims.email_verified:
                raise SsoRefusal(CREATION_UNVERIFIED)
        else:
            if not claims.email_verified:
                raise SsoRefusal(EMAIL_UNVERIFIED)
            if not settings.link_by_email:
                raise SsoRefusal(LINK_OFF)
            if await _has_identity(db, user):
                raise SsoRefusal(ALREADY_LINKED)
        link = UserIdentity(issuer=issuer, subject=claims.subject)
    if user is not None and not user.is_active:
        raise SsoRefusal(USER_DISABLED)
    return user, link


async def sign_in_account(
    db: AsyncSession,
    settings: SsoSettings,
    issuer: str,
    claims: SsoClaims,
    attempt: Attempt,
    now: datetime,
) -> tuple[User, bool]:
    """Find, link or create the account, bring its name, email and group in line with the
    provider and return it with whether it was created now.

    Every refusal is checked before anything is written. The group change commits, as every
    change of a group does.
    """
    user, link = await _account_to_sign_in(db, settings, issuer, claims, attempt)
    group = group_for(claims.roles, settings.mappings)
    if group is None:
        raise SsoRefusal(NO_MATCHING_ROLE)
    actor = AuditActor.system(SSO_ACTOR)
    created = user is None
    try:
        if user is None:
            user = await create_sso_user(
                db,
                name=claims.name or FALLBACK_NAME,
                email=claims.email,
                group=group,
                actor=actor,
            )
            _attach(db, link, user, now)
        else:
            if link is not None:
                _attach(db, link, user, now)
                await _audit_link(db, actor, user, link)
            await _update_profile(db, actor, user, claims)
        await db.flush()
    except UserExistsError:
        attempt.user = None
        raise SsoRefusal(EMAIL_COLLISION) from None
    except IntegrityError:
        await db.rollback()
        attempt.user = None
        raise SsoRefusal(ALREADY_LINKED) from None
    attempt.user = user
    if user.group is not UserGroup.SUPERADMIN and user.group is not group:
        await set_user_group(db, user, group, actor=actor)
    return user, created


def _attach(db: AsyncSession, link: UserIdentity | None, user: User, now: datetime) -> None:
    """Add the link of an identity to the account."""
    if link is not None:
        link.user_id = user.id
        link.linked_at = now
        db.add(link)


async def _audit_link(db: AsyncSession, actor: AuditActor, user: User, link: UserIdentity) -> None:
    """Audit that an existing account was linked to an identity."""
    await record_actor_audit(
        db,
        actor,
        action="user.sso_link",
        result=AuditResult.SUCCESS,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"issuer": link.issuer},
    )


async def _update_profile(
    db: AsyncSession, actor: AuditActor, user: User, claims: SsoClaims
) -> None:
    """Follow the provider's name and email, and audit which of them changed."""
    changed: list[str] = []
    if claims.name and claims.name != user.name:
        user.name = claims.name
        changed.append("name")
    if claims.email.lower() != user.email.lower():
        user.email = claims.email
        changed.append("email")
    if changed:
        await record_actor_audit(
            db,
            actor,
            action="user.sso_update",
            result=AuditResult.SUCCESS,
            target_type="user",
            target_id=user.id,
            target_name=user.name,
            details={"changed": changed},
        )
