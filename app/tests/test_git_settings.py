"""The Git settings service: checking the form, and what a save forgets and keeps."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models.types import utc_now
from app.services.app_settings import (
    GIT_DEPLOY_KEY_KEY,
    GIT_TOKEN_KEY,
    GitSettings,
    has_secret,
    read_secret,
    save_section,
)
from app.services.audit import AuditActor
from app.services.git_hostkey import HostKey
from app.services.git_locations import GitLocations
from app.services.git_settings import (
    GitForm,
    GitFormError,
    format_options,
    save_git,
    validate_git,
)
from app.services.git_state import load_state, pin_host_key, record_failure, record_success
from app.tests.git_helpers import generate_key

KEY_TEXT = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
ACTOR = AuditActor.cli()


def form(**changes: object) -> GitForm:
    """A valid form with these changes applied."""
    values: dict[str, object] = {
        "url": "https://ops@git.example.com/ops/env.git",
        "branch": "main",
        "author_name": "ENV Manager",
        "export_format": "per_version",
        "push_enabled": True,
    }
    values.update(changes)
    return GitForm(**values)  # type: ignore[arg-type]


def test_a_valid_form_becomes_settings_and_an_empty_address_switches_sync_off() -> None:
    """The text is trimmed; without an address the settings are valid and not configured."""
    clean = validate_git(form(url="  https://ops@git.example.com/ops/env.git  ", branch=" dev "))
    assert clean.settings == GitSettings(
        url="https://ops@git.example.com/ops/env.git", branch="dev"
    )
    assert clean.settings.configured
    off = validate_git(form(url=""))
    assert not off.settings.configured


def test_a_pasted_key_is_stored_with_unix_line_ends_and_a_final_newline() -> None:
    """A browser sends CRLF and an editor may drop the last line break; ssh needs neither."""
    clean = validate_git(form(deploy_key=KEY_TEXT.replace("\n", "\r\n") + "  "))
    assert clean.deploy_key == KEY_TEXT + "\n"
    assert validate_git(form(token="  tok  ")).token == "tok"


@pytest.mark.parametrize("branch", ["main", "release/1.0", "feature/a-b_c", "v1.2.3", "a"])
def test_branch_names_that_git_accepts(branch: str) -> None:
    """Ordinary branch names pass."""
    assert validate_git(form(branch=branch)).settings.branch == branch


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "-x",
        "a..b",
        "a//b",
        "a b",
        "x.lock",
        "/x",
        "x/",
        ".x",
        "a@{b",
        "a~b",
        "a:b",
        "a^b",
        "a\\b",
        "a*b",
        "a?b",
        "a[b",
        "x/.hidden",
        "a.lock/b",
        "é",
        "a" * 101,
    ],
)
def test_branch_names_git_would_refuse_or_read_as_something_else(branch: str) -> None:
    """Names with spaces, double dots, revision syntax, a leading dash and the like are refused."""
    with pytest.raises(GitFormError) as error:
        validate_git(form(branch=branch))
    assert set(error.value.errors) == {"branch"}


@pytest.mark.parametrize("name", ["", "a" * 101, "x\ny", "<x>", "x>", "x\x00y"])
def test_author_names_that_would_break_a_commit_header(name: str) -> None:
    """A line break or angle brackets would corrupt the author line."""
    with pytest.raises(GitFormError) as error:
        validate_git(form(author_name=name))
    assert set(error.value.errors) == {"author_name"}


def test_every_refusal_is_reported_at_once() -> None:
    """All the refused fields are in one error, so the form shows them together."""
    with pytest.raises(GitFormError) as error:
        validate_git(
            form(
                url="http://x.example.com/a.git",
                branch="..",
                author_name="",
                export_format="zip",
                deploy_key="ssh-rsa AAAA",
                token="a b",
            )
        )
    assert set(error.value.errors) == {
        "url",
        "branch",
        "author_name",
        "export_format",
        "deploy_key",
        "token",
    }


def test_the_export_formats_offered_are_the_two_of_the_decision() -> None:
    """One file per version first, then the database snapshot."""
    assert format_options() == [
        ("per_version", "One file per version"),
        ("snapshot", "Encrypted database snapshot"),
    ]


async def save(
    engine: AsyncEngine,
    crypto: CryptoService,
    tmp_path: Path,
    **changes: object,
) -> bool:
    """Save a form and return whether anything changed."""
    async with create_session_factory(engine)() as db:
        return await save_git(
            db,
            crypto,
            validate_git(form(**changes)),
            actor=ACTOR,
            locations=GitLocations(tmp_path / "git"),
        )


async def test_both_credentials_are_sealed_kept_replaced_and_removed(
    engine: AsyncEngine, crypto: CryptoService, tmp_path: Path
) -> None:
    """A credential is written when given, kept when the field is empty, replaced by a new one
    and deleted by the removal choice, independently of the other."""
    factory = create_session_factory(engine)
    assert await save(engine, crypto, tmp_path, deploy_key=KEY_TEXT, token="tok-1")
    async with factory() as db:
        assert await read_secret(db, crypto, GIT_DEPLOY_KEY_KEY) == KEY_TEXT + "\n"
        assert await read_secret(db, crypto, GIT_TOKEN_KEY) == "tok-1"
    assert not await save(engine, crypto, tmp_path)
    async with factory() as db:
        assert await read_secret(db, crypto, GIT_TOKEN_KEY) == "tok-1"
    assert await save(engine, crypto, tmp_path, token="tok-2")
    assert await save(engine, crypto, tmp_path, remove_deploy_key=True)
    async with factory() as db:
        assert await read_secret(db, crypto, GIT_TOKEN_KEY) == "tok-2"
        assert not await has_secret(db, GIT_DEPLOY_KEY_KEY)
    assert not await save(engine, crypto, tmp_path, remove_deploy_key=True)


async def seeded_state(engine: AsyncEngine, tmp_path: Path, *, failing: bool = True) -> HostKey:
    """A saved repository that pushed once, then failed, with a pinned host key."""
    _private, public = generate_key(tmp_path, "host")
    key_type, blob = public.split()[:2]
    key = HostKey("git.example.com", key_type, blob)
    async with create_session_factory(engine)() as db:
        await save_section(
            db, "git", GitSettings(url="ssh://git@git.example.com/ops/env.git"), actor=ACTOR
        )
        state = await load_state(db)
        record_success(state, "mark-1", utc_now() - timedelta(hours=3), pushed=True)
        if failing:
            record_failure(state, "The repository refused the credentials.", utc_now())
        pin_host_key(state, key, utc_now())
        await db.commit()
    return key


async def test_a_new_branch_or_format_forgets_the_watermark_and_keeps_the_pin(
    engine: AsyncEngine, crypto: CryptoService, tmp_path: Path
) -> None:
    """Pushing to another branch or in another layout starts over; the host is still the same."""
    await seeded_state(engine, tmp_path)
    url = "ssh://git@git.example.com/ops/env.git"
    assert await save(engine, crypto, tmp_path, url=url, branch="release")
    async with create_session_factory(engine)() as db:
        state = await load_state(db)
        assert (state.watermark, state.last_success_at) == (None, None)
        assert (state.failures, state.last_error, state.retry_at) == (0, None, None)
        assert state.host_key is not None
        state.watermark = "mark-2"
        await db.commit()
    assert await save(engine, crypto, tmp_path, url=url, branch="release", export_format="snapshot")
    async with create_session_factory(engine)() as db:
        assert (await load_state(db)).watermark is None


async def test_a_new_credential_forgets_the_failures_but_keeps_what_was_pushed(
    engine: AsyncEngine, crypto: CryptoService, tmp_path: Path
) -> None:
    """After a fixed credential the next run tries at once, but nothing is pushed again for it."""
    await seeded_state(engine, tmp_path)
    url = "ssh://git@git.example.com/ops/env.git"
    assert await save(engine, crypto, tmp_path, url=url, deploy_key=KEY_TEXT)
    async with create_session_factory(engine)() as db:
        state = await load_state(db)
        assert (state.failures, state.last_error, state.retry_at) == (0, None, None)
        assert state.failure_notified_at is None, "the streak ends without a recovery notice"
        assert state.watermark == "mark-1" and state.last_success_at is not None
        assert state.host_key is not None


async def test_an_unchanged_save_leaves_the_failure_state_alone(
    engine: AsyncEngine, crypto: CryptoService, tmp_path: Path
) -> None:
    """Saving the same values is not a fix, so the banner and the wait stay."""
    await seeded_state(engine, tmp_path)
    assert not await save(engine, crypto, tmp_path, url="ssh://git@git.example.com/ops/env.git")
    async with create_session_factory(engine)() as db:
        state = await load_state(db)
        assert state.failures == 1 and state.retry_at is not None
