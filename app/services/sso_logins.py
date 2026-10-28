"""Single sign-on attempts in progress: created when the person is sent to the provider and used
up, once, when they come back.

Only hashes of the state and of the browser binding token are stored, so reading the table gives
no way to finish an attempt. The PKCE code verifier is sealed with the master key, bound to the
state hash of its own row, so a sealed verifier cannot be moved to another row.
"""

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.models import SsoLogin
from app.models.types import utc_now
from app.services.sessions import TOKEN_BYTES, hash_token
from app.services.sso_refusal import BINDING_MISMATCH, STATE_INVALID, SsoRefusal

SSO_LOGIN_LIFETIME = timedelta(minutes=5)
VERIFIER_BYTES = 48


@dataclass(frozen=True)
class NewSsoLogin:
    """What starting an attempt hands out: ``state`` and ``nonce`` and the PKCE ``verifier`` go
    into the request to the provider, ``binding_token`` into a cookie of the browser."""

    state: str
    binding_token: str
    nonce: str
    verifier: str


@dataclass(frozen=True)
class UsedSsoLogin:
    """What an attempt held when it was used up."""

    nonce: str
    verifier: str


def verifier_context(state_hash: str) -> bytes:
    """The authenticated context that binds a sealed code verifier to its row."""
    return f"sso_login:{state_hash}".encode()


async def create_sso_login(
    db: AsyncSession, crypto: CryptoService, *, now: datetime | None = None
) -> NewSsoLogin:
    """Store a new attempt valid for five minutes and return what it needs.

    Expired attempts are removed on the way. The caller commits.
    """
    moment = now or utc_now()
    await db.execute(delete(SsoLogin).where(SsoLogin.expires_at <= moment))
    login = NewSsoLogin(
        state=secrets.token_urlsafe(TOKEN_BYTES),
        binding_token=secrets.token_urlsafe(TOKEN_BYTES),
        nonce=secrets.token_urlsafe(TOKEN_BYTES),
        verifier=secrets.token_urlsafe(VERIFIER_BYTES),
    )
    state_hash = hash_token(login.state)
    db.add(
        SsoLogin(
            state_hash=state_hash,
            binding_hash=hash_token(login.binding_token),
            nonce=login.nonce,
            code_verifier_encrypted=crypto.encrypt(
                login.verifier, context=verifier_context(state_hash)
            ),
            created_at=moment,
            expires_at=moment + SSO_LOGIN_LIFETIME,
        )
    )
    await db.flush()
    return login


async def consume_sso_login(
    db: AsyncSession,
    crypto: CryptoService,
    *,
    state: str,
    binding_token: str,
    now: datetime | None = None,
) -> UsedSsoLogin:
    """Use up the attempt of a state and return what it held, or raise ``SsoRefusal``.

    The row is deleted in one statement and committed, so an attempt can be used only once and a
    state that was presented is gone whatever the outcome. An unknown, used or expired state is
    refused, and so is a browser that does not hold the binding token of the attempt.
    """
    moment = now or utc_now()
    state_hash = hash_token(state) if state else ""
    row = (
        await db.execute(
            delete(SsoLogin)
            .where(SsoLogin.state_hash == state_hash)
            .returning(
                SsoLogin.binding_hash,
                SsoLogin.nonce,
                SsoLogin.code_verifier_encrypted,
                SsoLogin.expires_at,
            )
        )
    ).one_or_none()
    await db.commit()
    if row is None or row.expires_at <= moment:
        raise SsoRefusal(STATE_INVALID)
    if not binding_token or not hmac.compare_digest(hash_token(binding_token), row.binding_hash):
        raise SsoRefusal(BINDING_MISMATCH)
    try:
        verifier = crypto.decrypt_text(
            row.code_verifier_encrypted, context=verifier_context(state_hash)
        )
    except CryptoError:
        raise SsoRefusal(STATE_INVALID) from None
    return UsedSsoLogin(row.nonce, verifier)


async def purge_expired_sso_logins(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Remove every expired attempt and return how many went. The caller commits."""
    result = await db.execute(delete(SsoLogin).where(SsoLogin.expires_at <= (now or utc_now())))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
