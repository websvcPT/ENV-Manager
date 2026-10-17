"""Field encryption, keyed content hashing and master key handling.

Values are sealed with AES-256-GCM using a random nonce per encryption. Every ciphertext carries
a version tag so the key can rotate: decryption selects the key by the tag. Two independent
subkeys (encryption and hashing) are derived from the master key with HKDF, so the HMAC key is
never the encryption key.
"""

import base64
import binascii
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

KEY_BYTES = 32
NONCE_BYTES = 12
CURRENT_KEY_VERSION = 1
TAG_PREFIX = "v"
TAG_SEPARATOR = "$"
KEY_CHECK_PLAINTEXT = b"env-manager-key-check"
KEY_CHECK_CONTEXT = b"key-check"
_ENCRYPTION_INFO = b"env-manager/encryption"
_HASHING_INFO = b"env-manager/content-hash"


class CryptoError(Exception):
    """Base class for crypto failures. Messages never contain keys or plaintext."""


class KeyConfigurationError(CryptoError):
    """The master key is missing, unreadable or malformed."""


class KeyMismatchError(CryptoError):
    """The configured master key does not match the one the data was written with."""


class DecryptionError(CryptoError):
    """A ciphertext is malformed, tampered with, or sealed with another key."""


def generate_master_key() -> str:
    """Return a new random master key encoded as urlsafe base64."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def parse_master_key(encoded: str) -> bytes:
    """Decode a base64 master key and require exactly 32 bytes."""
    text = encoded.strip()
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError) as error:
        raise KeyConfigurationError("master key is not valid base64") from error
    if len(raw) != KEY_BYTES:
        raise KeyConfigurationError(f"master key must decode to exactly {KEY_BYTES} bytes")
    return raw


def load_master_key(key: SecretStr | None, key_file: Path | None) -> bytes:
    """Resolve the master key from the key file or the environment value.

    Exactly one source must be configured. Supplying both is refused so a stale value can
    never silently shadow the mounted key.
    """
    has_value = key is not None and bool(key.get_secret_value().strip())
    if has_value and key_file is not None:
        raise KeyConfigurationError("configure either the master key or the key file, not both")
    if key_file is not None:
        try:
            content = key_file.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as error:
            raise KeyConfigurationError("master key file cannot be read") from error
        return parse_master_key(content)
    if key is not None and has_value:
        return parse_master_key(key.get_secret_value())
    raise KeyConfigurationError("no master key configured")


def _derive(master_key: bytes, info: bytes) -> bytes:
    """Derive a 32 byte subkey for one purpose from the master key."""
    return HKDF(algorithm=SHA256(), length=KEY_BYTES, salt=None, info=info).derive(master_key)


class CryptoService:
    """Encrypts, decrypts and hashes values for one master key, tagged with a key version."""

    def __init__(self, master_key: bytes, version: int = CURRENT_KEY_VERSION) -> None:
        """Derive the encryption and hashing subkeys from a 32 byte master key."""
        if len(master_key) != KEY_BYTES:
            raise KeyConfigurationError(f"master key must be exactly {KEY_BYTES} bytes")
        if version < 1:
            raise KeyConfigurationError("key version must be a positive integer")
        self.version = version
        self._aead = AESGCM(_derive(master_key, _ENCRYPTION_INFO))
        self._hash_key = _derive(master_key, _HASHING_INFO)

    @property
    def tag(self) -> str:
        """The version tag written in front of every ciphertext."""
        return f"{TAG_PREFIX}{self.version}{TAG_SEPARATOR}"

    def encrypt(self, plaintext: str | bytes, context: bytes = b"") -> str:
        """Seal a value; ``context`` is authenticated and must be repeated to decrypt."""
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, data, context or None)
        return self.tag + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, token: str, context: bytes = b"") -> bytes:
        """Open a ciphertext produced by ``encrypt`` and return the plaintext bytes."""
        version, body = split_ciphertext(token)
        if version != self.version:
            raise DecryptionError(f"ciphertext uses key version {version}, not {self.version}")
        try:
            blob = base64.urlsafe_b64decode(body.encode("ascii"))
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise DecryptionError("ciphertext body is not valid base64") from error
        if len(blob) <= NONCE_BYTES:
            raise DecryptionError("ciphertext is too short")
        nonce, sealed = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, sealed, context or None)
        except InvalidTag as error:
            raise DecryptionError("ciphertext failed authentication") from error

    def decrypt_text(self, token: str, context: bytes = b"") -> str:
        """Open a ciphertext and decode it as UTF-8 text."""
        try:
            return self.decrypt(token, context).decode("utf-8")
        except UnicodeDecodeError as error:
            raise DecryptionError("plaintext is not valid UTF-8") from error

    def content_hash(self, content: str | bytes) -> str:
        """Return the keyed hash (hex HMAC-SHA256) used to compare content without leaking it."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        return hmac.new(self._hash_key, data, hashlib.sha256).hexdigest()

    def make_key_check(self) -> str:
        """Create the record stored in the database to detect a wrong key at startup."""
        return self.encrypt(KEY_CHECK_PLAINTEXT, KEY_CHECK_CONTEXT)

    def verify_key_check(self, record: str) -> None:
        """Raise ``KeyMismatchError`` unless the record was sealed with this key."""
        try:
            plaintext = self.decrypt(record, KEY_CHECK_CONTEXT)
        except DecryptionError as error:
            raise KeyMismatchError("master key does not match the stored key check") from error
        if not hmac.compare_digest(plaintext, KEY_CHECK_PLAINTEXT):
            raise KeyMismatchError("master key does not match the stored key check")


def split_ciphertext(token: str) -> tuple[int, str]:
    """Split a ciphertext into its key version and base64 body."""
    prefix, separator, body = token.partition(TAG_SEPARATOR)
    if not separator or not prefix.startswith(TAG_PREFIX) or not body:
        raise DecryptionError("ciphertext has no version tag")
    digits = prefix[len(TAG_PREFIX) :]
    if not digits.isascii() or not digits.isdigit():
        raise DecryptionError("ciphertext has a malformed version tag")
    return int(digits), body


def build_crypto_service(key: SecretStr | None, key_file: Path | None) -> CryptoService:
    """Load the master key from configuration and return a ready service."""
    return CryptoService(load_master_key(key, key_file))
