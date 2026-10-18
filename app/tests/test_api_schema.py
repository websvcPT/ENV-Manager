"""The strict body of a deploy request."""

import pytest

from app.schemas.api import (
    FORCE_MESSAGE,
    MAX_BODY_BYTES,
    MAX_ISSUES,
    DeployBody,
    InvalidBody,
    parse_deploy_body,
)


def test_every_field_is_optional_and_a_missing_body_is_empty() -> None:
    """Nothing, an empty object and nulls all mean the defaults."""
    for raw in (b"", b"  \n", b"{}", b'{"environments": null, "versions": null, "dry_run": null}'):
        body = parse_deploy_body(raw)
        assert (body.environments, body.versions, body.dry_run) == (None, None, None)


def test_a_full_body_is_accepted_as_written() -> None:
    """Names and ids, versions by name or id text, and the dry run flag."""
    body = parse_deploy_body(
        b'{"environments": [".env.api", 7], "versions": {".env.api": 2, "7": 1}, "dry_run": true}'
    )
    assert body == DeployBody(
        environments=[".env.api", 7], versions={".env.api": 2, "7": 1}, dry_run=True
    )


def issues(raw: bytes | None) -> dict[str, str]:
    """The refusal of a body by field name."""
    with pytest.raises(InvalidBody) as refused:
        parse_deploy_body(raw)
    return {issue.field: issue.message for issue in refused.value.issues}


def test_force_has_its_own_message_whatever_its_value() -> None:
    """The key is refused for true, false and null alike, with the API's explanation."""
    for value in (b"true", b"false", b"null", b'"yes"', b"0"):
        assert issues(b'{"force": ' + value + b"}") == {"force": FORCE_MESSAGE}


def test_other_unknown_keys_and_wrong_types_are_refused_in_plain_words() -> None:
    """An unknown key, a wrong type and a body that is not an object each say what is wrong."""
    assert issues(b'{"surprise": 1}') == {"surprise": "This field is not accepted."}
    assert issues(b'{"dry_run": "yes"}') == {"dry_run": "Must be true or false."}
    assert issues(b'{"environments": "api"}') == {
        "environments": "Must be a list of environment names or ids."
    }
    assert issues(b'{"versions": {"a": 1.5}}') == {
        "versions": "Each version must be a whole number."
    }
    assert issues(b"[1]") == {"body": "The body must be a JSON object."}
    assert issues(b"{") == {"body": "The body must be valid JSON."}
    assert issues(None) == {"body": "The body is too large."}


def test_the_issues_are_capped_and_never_echo_the_input() -> None:
    """A body full of unknown keys reports a bounded number of problems, without their values."""
    raw = ("{" + ",".join(f'"k{number}": "TOPSECRET"' for number in range(100)) + "}").encode()
    assert len(raw) < MAX_BODY_BYTES
    with pytest.raises(InvalidBody) as refused:
        parse_deploy_body(raw)
    assert len(refused.value.issues) == MAX_ISSUES
    assert "TOPSECRET" not in repr(refused.value.issues)
