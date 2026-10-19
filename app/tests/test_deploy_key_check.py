"""The deploy key check refuses a key that is sealed with a passphrase, whatever its format."""

from pathlib import Path

import pytest

from app.services.git_auth import deploy_key_problem, normalize_deploy_key
from app.tests.git_helpers import generate_key

PASSPHRASE_MESSAGE = "The key must not have a passphrase."


def test_a_generated_openssh_key_without_a_passphrase_is_accepted(tmp_path: Path) -> None:
    private, _public = generate_key(tmp_path, "plain")
    assert private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert deploy_key_problem(normalize_deploy_key(private)) is None


def test_a_generated_openssh_key_with_a_passphrase_is_refused(tmp_path: Path) -> None:
    private, _public = generate_key(tmp_path, "sealed", passphrase="correct horse battery")
    assert private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert "ENCRYPTED" not in private.splitlines()[0], "the header does not say it is sealed"
    assert deploy_key_problem(normalize_deploy_key(private)) == PASSPHRASE_MESSAGE


@pytest.mark.parametrize("key_type", ["ed25519", "rsa"])
def test_a_key_with_a_passphrase_is_refused_with_the_line_endings_a_browser_sends(
    tmp_path: Path, key_type: str
) -> None:
    private, _public = generate_key(tmp_path, "crlf", passphrase="a passphrase", key_type=key_type)
    browser_text = private.replace("\n", "\r\n")
    assert deploy_key_problem(normalize_deploy_key(browser_text)) == PASSPHRASE_MESSAGE


def test_a_pem_key_is_judged_by_its_header_and_its_encryption_line(tmp_path: Path) -> None:
    plain, _ = generate_key(tmp_path, "pem-plain", key_type="ecdsa", pem=True)
    sealed, _ = generate_key(
        tmp_path, "pem-sealed", passphrase="a passphrase", key_type="ecdsa", pem=True
    )
    assert plain.startswith("-----BEGIN EC PRIVATE KEY-----")
    assert deploy_key_problem(normalize_deploy_key(plain)) is None
    assert deploy_key_problem(normalize_deploy_key(sealed)) == PASSPHRASE_MESSAGE


def test_a_body_that_cannot_be_read_is_left_to_the_other_checks() -> None:
    header = "-----BEGIN OPENSSH PRIVATE KEY-----"
    footer = "-----END OPENSSH PRIVATE KEY-----"
    for body in ("abc", "not base64 at all!", "ZTJlLWdpdC1kZXBsb3kta2V5LXNlbnRpbmVs"):
        assert deploy_key_problem(f"{header}\n{body}\n{footer}\n") is None
