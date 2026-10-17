"""Tests for field encryption, content hashing and master key loading."""

import base64
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.crypto import (
    CryptoService,
    DecryptionError,
    KeyConfigurationError,
    KeyMismatchError,
    build_crypto_service,
    generate_master_key,
    load_master_key,
    parse_master_key,
    split_ciphertext,
)


@pytest.fixture
def service() -> CryptoService:
    """A service built from a fresh random key."""
    return CryptoService(parse_master_key(generate_master_key()))


def test_round_trip_text_and_bytes(service: CryptoService) -> None:
    """Text and bytes come back unchanged, including non-ASCII and multiline content."""
    text = "DB_PASSWORD=pa ss\nNAME=café\n"
    assert service.decrypt_text(service.encrypt(text)) == text
    assert service.decrypt(service.encrypt(b"\x00\xff")) == b"\x00\xff"
    assert service.decrypt_text(service.encrypt("")) == ""


def test_ciphertext_is_tagged_and_hides_plaintext(service: CryptoService) -> None:
    """The version tag leads the token and the plaintext is not visible in it."""
    token = service.encrypt("supersecretvalue")
    assert token.startswith("v1$")
    assert "supersecretvalue" not in token
    assert split_ciphertext(token)[0] == 1


def test_nonce_is_random_per_encryption(service: CryptoService) -> None:
    """Encrypting the same value twice gives different ciphertexts."""
    assert service.encrypt("same") != service.encrypt("same")


def test_context_is_authenticated(service: CryptoService) -> None:
    """A ciphertext only opens with the context it was sealed with."""
    token = service.encrypt("value", b"env:1")
    assert service.decrypt_text(token, b"env:1") == "value"
    with pytest.raises(DecryptionError):
        service.decrypt(token, b"env:2")
    with pytest.raises(DecryptionError):
        service.decrypt(token)


def test_tampering_is_detected(service: CryptoService) -> None:
    """Flipping a byte of the sealed body fails authentication."""
    token = service.encrypt("value")
    tag, body = token.split("$", 1)
    raw = bytearray(base64.urlsafe_b64decode(body))
    raw[-1] ^= 0x01
    forged = f"{tag}${base64.urlsafe_b64encode(bytes(raw)).decode()}"
    with pytest.raises(DecryptionError):
        service.decrypt(forged)


@pytest.mark.parametrize(
    "token",
    ["", "garbage", "v1$", "x1$abcd", "v$abcd", "v1x$abcd", "v1$!!!!", "v1$QUJD", "v2$QUJDRA=="],
)
def test_malformed_ciphertext_is_rejected(service: CryptoService, token: str) -> None:
    """Missing tags, bad versions, bad base64 and short bodies all raise DecryptionError."""
    with pytest.raises(DecryptionError):
        service.decrypt(token)


def test_wrong_key_cannot_decrypt(service: CryptoService) -> None:
    """A ciphertext does not open under a different master key."""
    other = CryptoService(parse_master_key(generate_master_key()))
    with pytest.raises(DecryptionError):
        other.decrypt(service.encrypt("value"))


def test_invalid_utf8_plaintext_is_reported(service: CryptoService) -> None:
    """decrypt_text refuses bytes that are not UTF-8."""
    with pytest.raises(DecryptionError):
        service.decrypt_text(service.encrypt(b"\xff\xfe"))


def test_content_hash_is_keyed_and_stable(service: CryptoService) -> None:
    """The hash is deterministic per key, differs across keys and content, and is hex."""
    other = CryptoService(parse_master_key(generate_master_key()))
    first = service.content_hash("A=1\n")
    assert first == service.content_hash("A=1\n") == service.content_hash(b"A=1\n")
    assert first != service.content_hash("A=2\n")
    assert first != other.content_hash("A=1\n")
    assert len(first) == 64
    int(first, 16)


def test_hash_key_is_independent_of_encryption_key(service: CryptoService) -> None:
    """Hash output does not appear in or derive from ciphertext material."""
    assert service.content_hash("x") not in service.encrypt("x")


def test_key_check_accepts_same_key_and_rejects_others(service: CryptoService) -> None:
    """The key check verifies for the creating key and raises for a different one."""
    record = service.make_key_check()
    service.verify_key_check(record)
    other = CryptoService(parse_master_key(generate_master_key()))
    with pytest.raises(KeyMismatchError):
        other.verify_key_check(record)
    with pytest.raises(KeyMismatchError):
        service.verify_key_check("not a record")


def test_key_check_is_bound_to_its_context(service: CryptoService) -> None:
    """A value sealed without the key check context is not accepted as a key check."""
    with pytest.raises(KeyMismatchError):
        service.verify_key_check(service.encrypt(b"env-manager-key-check"))


def test_generated_key_parses_to_32_bytes() -> None:
    """Generated keys round trip through the parser and differ each time."""
    first, second = generate_master_key(), generate_master_key()
    assert first != second
    assert len(parse_master_key(first)) == 32


@pytest.mark.parametrize("value", ["", "!!!", "QUJD", base64.b64encode(b"x" * 31).decode()])
def test_bad_master_keys_are_rejected(value: str) -> None:
    """Non-base64 and wrong-length keys fail with a configuration error."""
    with pytest.raises(KeyConfigurationError):
        parse_master_key(value)


def test_service_rejects_wrong_length_key() -> None:
    """The service requires exactly 32 key bytes and a positive version."""
    with pytest.raises(KeyConfigurationError):
        CryptoService(b"short")
    with pytest.raises(KeyConfigurationError):
        CryptoService(b"x" * 32, version=0)


def test_load_from_environment_value() -> None:
    """The key can come from the environment value, with surrounding whitespace tolerated."""
    encoded = generate_master_key()
    assert load_master_key(SecretStr(f" {encoded}\n"), None) == parse_master_key(encoded)


def test_load_from_key_file(tmp_path: Path) -> None:
    """The key can come from a mounted file with a trailing newline."""
    encoded = generate_master_key()
    key_file = tmp_path / "master.key"
    key_file.write_text(encoded + "\n", encoding="ascii")
    assert load_master_key(None, key_file) == parse_master_key(encoded)


def test_missing_key_is_refused(tmp_path: Path) -> None:
    """No source, an empty value, or an absent file all refuse startup."""
    with pytest.raises(KeyConfigurationError):
        load_master_key(None, None)
    with pytest.raises(KeyConfigurationError):
        load_master_key(SecretStr("  "), None)
    with pytest.raises(KeyConfigurationError):
        load_master_key(None, tmp_path / "absent.key")


def test_both_sources_are_refused(tmp_path: Path) -> None:
    """Configuring the value and the file together is ambiguous and refused."""
    key_file = tmp_path / "master.key"
    key_file.write_text(generate_master_key(), encoding="ascii")
    with pytest.raises(KeyConfigurationError):
        load_master_key(SecretStr(generate_master_key()), key_file)


def test_error_messages_do_not_leak_the_key() -> None:
    """A malformed key never appears in the error text."""
    bad_key = "not-base64-value!"
    with pytest.raises(KeyConfigurationError) as caught:
        parse_master_key(bad_key)
    assert bad_key not in str(caught.value)


def test_build_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings feed the loader and the secret never prints in the settings repr."""
    encoded = generate_master_key()
    monkeypatch.setenv("ENVMANAGER_KEY", encoded)
    settings = Settings()
    assert encoded not in repr(settings)
    built = build_crypto_service(settings.key, settings.key_file)
    assert built.decrypt_text(built.encrypt("ok")) == "ok"
