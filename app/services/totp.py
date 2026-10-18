"""Optional TOTP second factor. The secret is stored encrypted, never in plain text."""

import io
from datetime import datetime

import pyotp
import segno
from pyotp.utils import strings_equal
from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.core.log import get_logger
from app.models import User
from app.models.types import utc_now

log = get_logger("totp")

ISSUER = "ENV Manager"
VALID_WINDOW = 1
QR_MODULE_SCALE = 4
QR_QUIET_ZONE = 4


def _context(user: User) -> bytes:
    """Authenticated context that binds a stored secret to its owner."""
    return f"user-totp:{user.id}".encode()


def generate_secret() -> str:
    """A new random base32 TOTP secret."""
    return pyotp.random_base32()


def provisioning_uri(user: User, secret: str) -> str:
    """The otpauth URI an authenticator app enrols from."""
    return pyotp.TOTP(secret).provisioning_uri(name=user.name, issuer_name=ISSUER)


def qr_svg(uri: str) -> bytes:
    """The QR code of an otpauth URI as a standalone SVG that scales to its box.

    Dark modules on a light ground with the standard quiet zone, whatever the page theme, because
    scanners need the contrast. The drawing uses attributes only, no inline style.
    """
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(
        buffer,
        kind="svg",
        scale=QR_MODULE_SCALE,
        border=QR_QUIET_ZONE,
        dark="#000",
        light="#fff",
        omitsize=True,
        nl=False,
    )
    return buffer.getvalue()


def store_secret(user: User, secret: str, crypto: CryptoService) -> None:
    """Keep a pending secret on the user as ciphertext. It is not enforced until enabled."""
    user.totp_secret = crypto.encrypt(secret, _context(user))
    user.totp_enabled = False
    user.totp_enabled_at = None
    user.totp_last_step = None


def read_secret(user: User, crypto: CryptoService) -> str | None:
    """The plaintext secret, or None when none is stored or it cannot be decrypted."""
    if not user.totp_secret:
        return None
    try:
        return crypto.decrypt_text(user.totp_secret, _context(user))
    except CryptoError:
        log.error("stored second factor secret for user %s cannot be decrypted", user.id)
        return None


def step_for_secret(secret: str, code: str, *, now: datetime | None = None) -> int | None:
    """The 30 second time step a code belongs to for a secret, or None outside the window.

    Callers that must refuse reuse record the step and compare it with later ones.
    """
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit():
        return None
    generator = pyotp.TOTP(secret)
    current = generator.timecode(now or utc_now())
    matched: int | None = None
    for step in range(current - VALID_WINDOW, current + VALID_WINDOW + 1):
        if strings_equal(cleaned, generator.generate_otp(step)):
            matched = step
    return matched


def matching_step(
    user: User, code: str, crypto: CryptoService, *, now: datetime | None = None
) -> int | None:
    """The time step a code belongs to for the user's stored secret, or None."""
    secret = read_secret(user, crypto)
    return None if secret is None else step_for_secret(secret, code, now=now)


def verify_code(user: User, code: str, crypto: CryptoService) -> bool:
    """Whether a code from the authenticator matches the user's stored secret."""
    return matching_step(user, code, crypto) is not None


async def claim_step(db: AsyncSession, user: User, step: int) -> bool:
    """Record a time step as used unless it, or a later one, already was. Atomic in one UPDATE."""
    claimed = await db.execute(
        update(User)
        .where(User.id == user.id, or_(User.totp_last_step.is_(None), User.totp_last_step < step))
        .values(totp_last_step=step)
    )
    return claimed.rowcount == 1  # type: ignore[attr-defined, no-any-return]


def enable(user: User, code: str, crypto: CryptoService, *, now: datetime | None = None) -> bool:
    """Turn the second factor on when the confirmation code is valid.

    The confirmation code counts as used, so it cannot be replayed at the next sign in.
    """
    step = matching_step(user, code, crypto, now=now)
    if step is None:
        return False
    user.totp_enabled = True
    user.totp_enabled_at = now or utc_now()
    user.totp_last_step = step
    return True


def activate(
    user: User, secret: str, step: int, crypto: CryptoService, *, now: datetime | None = None
) -> None:
    """Store a confirmed secret as ciphertext and turn the second factor on.

    The step of the confirmation code counts as used. This is the first time the secret reaches
    the user row: until a code proved it, a pending secret lives only in the setup cookie.
    """
    store_secret(user, secret, crypto)
    user.totp_enabled = True
    user.totp_enabled_at = now or utc_now()
    user.totp_last_step = step


def disable(user: User) -> None:
    """Turn the second factor off and discard the secret."""
    user.totp_enabled = False
    user.totp_enabled_at = None
    user.totp_secret = None
    user.totp_last_step = None
