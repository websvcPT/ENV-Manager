"""Optional TOTP second factor. The secret is stored encrypted, never in plain text."""

import pyotp

from app.core.crypto import CryptoError, CryptoService
from app.core.log import get_logger
from app.models import User

log = get_logger("totp")

ISSUER = "ENV Manager"
VALID_WINDOW = 1


def _context(user: User) -> bytes:
    """Authenticated context that binds a stored secret to its owner."""
    return f"user-totp:{user.id}".encode()


def generate_secret() -> str:
    """A new random base32 TOTP secret."""
    return pyotp.random_base32()


def provisioning_uri(user: User, secret: str) -> str:
    """The otpauth URI an authenticator app enrols from."""
    return pyotp.TOTP(secret).provisioning_uri(name=user.name, issuer_name=ISSUER)


def store_secret(user: User, secret: str, crypto: CryptoService) -> None:
    """Keep a pending secret on the user as ciphertext. It is not enforced until enabled."""
    user.totp_secret = crypto.encrypt(secret, _context(user))
    user.totp_enabled = False


def read_secret(user: User, crypto: CryptoService) -> str | None:
    """The plaintext secret, or None when none is stored or it cannot be decrypted."""
    if not user.totp_secret:
        return None
    try:
        return crypto.decrypt_text(user.totp_secret, _context(user))
    except CryptoError:
        log.error("stored second factor secret for user %s cannot be decrypted", user.id)
        return None


def verify_code(user: User, code: str, crypto: CryptoService) -> bool:
    """Whether a code from the authenticator matches the user's stored secret."""
    secret = read_secret(user, crypto)
    cleaned = code.strip().replace(" ", "")
    if secret is None or not cleaned.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=VALID_WINDOW))


def enable(user: User, code: str, crypto: CryptoService) -> bool:
    """Turn the second factor on when the confirmation code is valid."""
    if not verify_code(user, code, crypto):
        return False
    user.totp_enabled = True
    return True


def disable(user: User) -> None:
    """Turn the second factor off and discard the secret."""
    user.totp_enabled = False
    user.totp_secret = None
