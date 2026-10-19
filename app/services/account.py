"""A signed-in user's own account: password change and the second factor.

Checks that prove a credential (the current password, also when turning the second factor on, and
an authenticator code when removing it) count their failures on the same email and address
counters as sign in, and are refused while either is locked. Every outcome is audited with counts
and reasons only: no password, code or secret is ever recorded.
"""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.passwords import hash_password, verify_password
from app.models import AuditResult, LoginCounterKind, User, UserSession
from app.models.types import utc_now
from app.services import totp
from app.services.audit import AuditActor, record_actor_audit
from app.services.auth_policy import AuthPolicy
from app.services.lockout import (
    announce_lockouts,
    current_lock,
    get_counter,
    record_lockouts,
    register_attempt_failure,
)
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    UserChange,
    UserChanged,
    announce_event,
)
from app.services.pending_logins import discard_user_pending_logins
from app.services.sessions import revoke_user_sessions

CREDENTIAL_CHANGE = "account.password_change"
TOTP_ENABLE = "account.totp_enable"
TOTP_DISABLE = "account.totp_disable"


class AccountError(Exception):
    """A refused account change. The message is safe to show and never holds a secret.

    ``field`` names the form field the message belongs to, when there is one.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class AccountLockedError(AccountError):
    """The email or client address is locked after too many failed attempts."""

    def __init__(self, retry_after: timedelta) -> None:
        super().__init__("Too many failed attempts.")
        self.retry_after = retry_after


async def _audit(
    db: AsyncSession,
    user: User,
    action: str,
    result: AuditResult,
    ip_address: str | None,
    details: dict[str, object] | None = None,
) -> None:
    """Add an audit entry for an action of the user on their own account. The caller commits."""
    await record_actor_audit(
        db,
        AuditActor.for_user(user, ip_address),
        action=action,
        result=result,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details=details,
    )


async def _refuse_when_locked(
    db: AsyncSession,
    user: User,
    action: str,
    ip_address: str | None,
    now: datetime,
) -> None:
    """Audit and refuse an attempt made while the email or address is locked."""
    wait = await current_lock(db, subject=user.email.lower(), ip_address=ip_address, now=now)
    if wait is None:
        return
    await _audit(db, user, action, AuditResult.BLOCKED, ip_address, {"reason": "locked"})
    await db.commit()
    raise AccountLockedError(wait)


async def _refuse_credential(
    db: AsyncSession,
    policy: AuthPolicy,
    user: User,
    action: str,
    *,
    reason: str,
    message: str,
    field: str,
    ip_address: str | None,
    now: datetime,
    notifier: EventNotifier,
) -> AccountError:
    """Count and audit a wrong credential, then return the error to raise.

    Reaching the limit locks the email or the address, which is raised as a lockout instead.
    """
    locked = await register_attempt_failure(
        db, policy, subject=user.email.lower(), ip_address=ip_address, now=now
    )
    await _audit(db, user, action, AuditResult.FAILURE, ip_address, {"reason": reason})
    await record_lockouts(db, policy, user=user, ip_address=ip_address, locked=locked)
    await db.commit()
    await announce_lockouts(notifier, policy, user=user, ip_address=ip_address, locked=locked)
    if locked:
        return AccountLockedError(policy.lockout_duration)
    return AccountError(message, field)


async def change_password(
    db: AsyncSession,
    policy: AuthPolicy,
    user: User,
    session: UserSession,
    *,
    current_password: str,
    new_password: str,
    confirm_password: str,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
    now: datetime | None = None,
) -> int:
    """Replace the user's password and end their other sessions. Returns how many ended.

    The current password must be right (a wrong one counts toward the lockout), the new one must
    meet the length rule, match its confirmation and differ from the current one. The session
    that made the change stays, and so does nothing else: sessions and unfinished second factor
    sign ins started with the old password end in the same commit.
    """
    moment = now or utc_now()
    await _refuse_when_locked(db, user, CREDENTIAL_CHANGE, ip_address, moment)
    if not current_password:
        raise AccountError("Enter your current password.", "current_password")
    matched, _ = verify_password(current_password, user.password_hash)
    if not matched:
        raise await _refuse_credential(
            db,
            policy,
            user,
            CREDENTIAL_CHANGE,
            reason="invalid_current_password",
            message="The current password is incorrect.",
            field="current_password",
            ip_address=ip_address,
            now=moment,
            notifier=notifier,
        )
    if not new_password:
        raise AccountError("Enter a new password.", "new_password")
    if len(new_password) < policy.min_password_length:
        raise AccountError(
            f"The new password must be at least {policy.min_password_length} characters.",
            "new_password",
        )
    if new_password == current_password:
        raise AccountError("The new password must differ from the current one.", "new_password")
    if confirm_password != new_password:
        raise AccountError("The passwords do not match.", "confirm_password")

    user.password_hash = hash_password(new_password)
    ended = await revoke_user_sessions(db, user.id, keep_session_id=session.id)
    await discard_user_pending_logins(db, user.id)
    counter = await get_counter(db, LoginCounterKind.USER, user.email.lower())
    if counter is not None:
        counter.failures = 0
        counter.locked_until = None
        counter.window_started_at = moment
    await _audit(
        db, user, CREDENTIAL_CHANGE, AuditResult.SUCCESS, ip_address, {"sessions_ended": ended}
    )
    await db.commit()
    await announce_event(notifier, UserChanged(UserChange.PASSWORD_CHANGED, user.name, user.name))
    return ended


async def enable_totp(
    db: AsyncSession,
    policy: AuthPolicy,
    user: User,
    crypto: CryptoService,
    *,
    secret: str,
    password: str,
    code: str,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
    now: datetime | None = None,
) -> None:
    """Turn the second factor on with the secret of a setup, after the password and a valid code.

    The current password is checked first (a session that was taken over cannot enrol another
    authenticator without it): a wrong one counts toward the lockout and the attempt is refused
    while the email or address is locked. The code then proves the secret was scanned; its time
    step counts as used. A wrong code is only refused, not counted: nothing is being guessed,
    since the secret is the one just shown to the user.
    """
    moment = now or utc_now()
    if user.totp_enabled:
        raise AccountError("Two-factor authentication is already on.")
    await _refuse_when_locked(db, user, TOTP_ENABLE, ip_address, moment)
    if not password:
        raise AccountError("Enter your current password.", "current_password")
    if not code.strip():
        raise AccountError("Enter the 6-digit code.", "code")
    matched, _ = verify_password(password, user.password_hash)
    if not matched:
        raise await _refuse_credential(
            db,
            policy,
            user,
            TOTP_ENABLE,
            reason="invalid_password",
            message="The current password is incorrect.",
            field="current_password",
            ip_address=ip_address,
            now=moment,
            notifier=notifier,
        )
    step = totp.step_for_secret(secret, code, now=moment)
    if step is None:
        raise AccountError("The code is incorrect or expired.", "code")
    totp.activate(user, secret, step, crypto, now=moment)
    await _audit(db, user, TOTP_ENABLE, AuditResult.SUCCESS, ip_address)
    await db.commit()


async def disable_totp(
    db: AsyncSession,
    policy: AuthPolicy,
    user: User,
    crypto: CryptoService,
    *,
    password: str,
    code: str,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
    now: datetime | None = None,
) -> None:
    """Turn the second factor off. Needs the current password and a valid, unused code.

    A wrong password, a wrong code and a code that was already used all count toward the lockout.
    """
    moment = now or utc_now()
    if not user.totp_enabled:
        raise AccountError("Two-factor authentication is not on.")
    await _refuse_when_locked(db, user, TOTP_DISABLE, ip_address, moment)
    if not password:
        raise AccountError("Enter your current password.", "current_password")
    if not code.strip():
        raise AccountError("Enter the 6-digit code.", "code")
    matched, _ = verify_password(password, user.password_hash)
    if not matched:
        raise await _refuse_credential(
            db,
            policy,
            user,
            TOTP_DISABLE,
            reason="invalid_password",
            message="The current password is incorrect.",
            field="current_password",
            ip_address=ip_address,
            now=moment,
            notifier=notifier,
        )
    step = totp.matching_step(user, code, crypto, now=moment)
    reason = "invalid_code"
    if step is not None:
        if await totp.claim_step(db, user, step):
            totp.disable(user)
            await _audit(db, user, TOTP_DISABLE, AuditResult.SUCCESS, ip_address)
            await db.commit()
            return
        reason = "reused_code"
    raise await _refuse_credential(
        db,
        policy,
        user,
        TOTP_DISABLE,
        reason=reason,
        message="The code is incorrect or expired.",
        field="code",
        ip_address=ip_address,
        now=moment,
        notifier=notifier,
    )
