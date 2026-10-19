"""The address of the repository: what is accepted, what is refused and what is read from it."""

import pytest

from app.services.app_settings import url_origin
from app.services.git_remote import RemoteError, RemoteKind, parse_remote


@pytest.mark.parametrize(
    ("address", "kind", "host", "port", "user", "known_hosts", "label"),
    [
        (
            "ssh://git@git.example.com/ops/env.git",
            "ssh",
            "git.example.com",
            None,
            "git",
            "git.example.com",
            "git.example.com",
        ),
        (
            "ssh://deploy@git.example.com:2222/ops/env.git",
            "ssh",
            "git.example.com",
            2222,
            "deploy",
            "[git.example.com]:2222",
            "git.example.com:2222",
        ),
        (
            "ssh://git.example.com:22/ops/env.git",
            "ssh",
            "git.example.com",
            22,
            "git",
            "git.example.com",
            "git.example.com:22",
        ),
        (
            "git@git.example.com:ops/env.git",
            "ssh",
            "git.example.com",
            None,
            "git",
            "git.example.com",
            "git.example.com",
        ),
        (
            "git.example.com:ops/env.git",
            "ssh",
            "git.example.com",
            None,
            "git",
            "git.example.com",
            "git.example.com",
        ),
        (
            "git@git.example.com:/srv/git/env.git",
            "ssh",
            "git.example.com",
            None,
            "git",
            "git.example.com",
            "git.example.com",
        ),
        (
            "ssh://git@[2001:db8::1]:2200/ops/env.git",
            "ssh",
            "[2001:db8::1]",
            2200,
            "git",
            "[2001:db8::1]:2200",
            "[2001:db8::1]:2200",
        ),
        (
            "git@[2001:db8::1]:ops/env.git",
            "ssh",
            "[2001:db8::1]",
            None,
            "git",
            "2001:db8::1",
            "[2001:db8::1]",
        ),
        (
            "https://git.example.com/ops/env.git",
            "https",
            "git.example.com",
            None,
            None,
            None,
            "git.example.com",
        ),
        (
            "https://ops@git.example.com:8443/ops/env.git",
            "https",
            "git.example.com",
            8443,
            "ops",
            None,
            "git.example.com:8443",
        ),
    ],
)
def test_the_accepted_addresses_and_what_is_read_from_them(
    address: str,
    kind: str,
    host: str,
    port: int | None,
    user: str | None,
    known_hosts: str | None,
    label: str,
) -> None:
    """The transport, host, port and user, the name a host key is filed under and the label."""
    remote = parse_remote(address)
    assert remote.kind is RemoteKind(kind)
    assert (remote.host, remote.port, remote.user) == (host, port, user)
    if known_hosts is not None:
        assert remote.known_hosts_name == known_hosts
    assert remote.label == label


def test_a_token_is_sent_with_the_user_of_the_address_or_a_neutral_one() -> None:
    """A host that wants a particular user (Bitbucket) gets it from the address."""
    assert parse_remote("https://x-token-auth@bitbucket.org/ops/env.git").token_user == (
        "x-token-auth"
    )
    assert parse_remote("https://github.com/ops/env.git").token_user == "x-access-token"


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "http://git.example.com/ops/env.git",
        "https://user:secret@git.example.com/ops/env.git",
        "https://:secret@git.example.com/ops/env.git",
        "ssh://git:secret@git.example.com/ops/env.git",
        "-oProxyCommand=touch/tmp/x",
        "--upload-pack=touch/tmp/x",
        "ext::sh -c touch% /tmp/x",
        "ext::sh",
        "fd::17",
        "file:///srv/git/env.git",
        "/srv/git/env.git",
        "git://git.example.com/ops/env.git",
        "ftp://git.example.com/ops/env.git",
        "https://git.example.com",
        "https://git.example.com/",
        "https://git.example.com/ops/env.git?token=abc",
        "https://git.example.com/ops/env.git#fragment",
        "ssh://git@git.example.com:99999/ops/env.git",
        "ssh://git@git.example.com:0/ops/env.git",
        "https://git.example.com:abc/ops/env.git",
        "https://git .example.com/ops/env.git",
        "https://git.example.com/ops/env.git\nsecond",
        "git@git.example.com",
        "git@:ops/env.git",
        "git@exa mple.com:ops/env.git",
        "bad user@git.example.com:ops/env.git",
        "ssh://-oProxyCommand=x@git.example.com/ops/env.git",
        "https://" + "a" * 3000 + "/x.git",
    ],
)
def test_refused_addresses(address: str) -> None:
    """Passwords, other transports, options, missing parts and control characters are refused."""
    with pytest.raises(RemoteError):
        parse_remote(address)


def test_a_refusal_never_repeats_the_address() -> None:
    """The message says what is wrong, not what was typed (it may hold a token)."""
    with pytest.raises(RemoteError) as error:
        parse_remote("https://user:hunter2-secret@git.example.com/ops/env.git")
    assert "hunter2" not in str(error.value) and "git.example.com" not in str(error.value)


def test_a_local_path_is_accepted_only_when_the_caller_asks_for_it() -> None:
    """Tests of the service layer use bare repositories in temporary folders."""
    assert parse_remote("/srv/git/env.git", allow_local=True).kind is RemoteKind.FILE
    assert parse_remote("file:///srv/git/env.git", allow_local=True).kind is RemoteKind.FILE
    assert parse_remote("/srv/git/env.git", allow_local=True).label == "local path"
    with pytest.raises(RemoteError):
        parse_remote("-x", allow_local=True)


@pytest.mark.parametrize(
    ("address", "origin"),
    [
        ("git@git.example.com:ops/env.git", "ssh://git.example.com"),
        ("git.example.com:ops/env.git", "ssh://git.example.com"),
        ("ssh://git@git.example.com:2222/ops/env.git", "ssh://git.example.com:2222"),
        ("https://ops@git.example.com/ops/env.git", "https://git.example.com"),
        ("/srv/git/env.git", ""),
        ("", ""),
    ],
)
def test_the_audit_form_of_an_address_keeps_scheme_and_host_only(address: str, origin: str) -> None:
    """A settings audit entry shows where the repository is, never its path or user."""
    assert url_origin(address) == origin
