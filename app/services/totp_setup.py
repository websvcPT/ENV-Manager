"""A second factor setup that no code has confirmed yet.

The pending secret is never written to the database. It is sealed with the crypto service,
bound to the user and the session that started the setup, and handed to the browser in an
HttpOnly cookie scoped to the setup routes. The secret reaches the user row only once a valid
code from the authenticator proves it was scanned.
"""

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.crypto import CryptoError, CryptoService
from app.models import User, UserSession
from app.models.types import utc_now
from app.services import totp

SETUP_LIFETIME = timedelta(minutes=10)
SETUP_ID_BYTES = 6


@dataclass(frozen=True)
class PendingSetup:
    """An unconfirmed secret and the id that tells one setup from the next in the QR address."""

    secret: str
    setup_id: str
    expires_at: datetime


def _context(user: User, session: UserSession) -> bytes:
    """Authenticated context that ties a sealed setup to one user and one session."""
    return f"totp-setup:{user.id}:{session.id}".encode()


def begin_setup(
    user: User, session: UserSession, crypto: CryptoService, *, now: datetime | None = None
) -> tuple[PendingSetup, str]:
    """Generate a new secret and return it with the sealed value for the cookie."""
    moment = now or utc_now()
    setup = PendingSetup(
        secret=totp.generate_secret(),
        setup_id=secrets.token_urlsafe(SETUP_ID_BYTES),
        expires_at=moment + SETUP_LIFETIME,
    )
    payload = json.dumps(
        {"secret": setup.secret, "id": setup.setup_id, "expires": setup.expires_at.timestamp()}
    )
    return setup, crypto.encrypt(payload, _context(user, session))


def read_setup(
    user: User,
    session: UserSession,
    sealed: str | None,
    crypto: CryptoService,
    *,
    now: datetime | None = None,
) -> PendingSetup | None:
    """The pending setup in a cookie value, or None when it is missing, forged or expired."""
    if not sealed:
        return None
    try:
        payload = json.loads(crypto.decrypt_text(sealed, _context(user, session)))
        expires_at = datetime.fromtimestamp(float(payload["expires"]), tz=(now or utc_now()).tzinfo)
        setup = PendingSetup(str(payload["secret"]), str(payload["id"]), expires_at)
    except CryptoError, ValueError, KeyError, TypeError:
        return None
    return setup if (now or utc_now()) < setup.expires_at else None
