"""The Git settings: checking the form and saving it.

The deploy key and the access token are secrets. They are never shown back: the form carries one
only when someone types or pastes a new one, an empty field keeps the stored one, and a separate
choice removes it. Which credential is used follows the address: the deploy key for an ssh
address, the token for an https address. A change of the repository address forgets everything
that belonged to the old repository: the pinned host key, the watermark, the failures and the
working clone. A change of the branch or of the export format makes the next push a full one,
and any change gives the next scheduled run a fresh start, so a fixed credential is tried at once.
"""

import asyncio
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AuditResult
from app.services.app_settings import (
    GIT_DEPLOY_KEY_KEY,
    GIT_TOKEN_KEY,
    GitSettings,
    clear_secret,
    save_section,
    write_secret,
)
from app.services.audit import AuditActor, record_actor_audit
from app.services.git_auth import deploy_key_problem, normalize_deploy_key
from app.services.git_export import EXPORT_FORMATS, PER_VERSION, SNAPSHOT
from app.services.git_locations import GitLocations
from app.services.git_remote import RemoteError, parse_remote
from app.services.git_state import (
    clear_host_key,
    forget_failures,
    load_state,
    reset_progress,
)

MAX_AUTHOR_LENGTH = 100
MAX_BRANCH_LENGTH = 100
MAX_TOKEN_LENGTH = 1024
FORMAT_LABELS: dict[str, str] = {
    PER_VERSION: "One file per version",
    SNAPSHOT: "Encrypted database snapshot",
}
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
ACTION_PIN_CLEAR = "git.host_key_clear"
TARGET_TYPE = "git"


@dataclass(frozen=True)
class GitForm:
    """The Git settings as typed.

    ``deploy_key`` and ``token`` are empty to keep what is stored; ``remove_deploy_key`` and
    ``remove_token`` ask to delete it.
    """

    url: str
    branch: str
    author_name: str
    export_format: str
    push_enabled: bool
    deploy_key: str = ""
    token: str = ""
    remove_deploy_key: bool = False
    remove_token: bool = False

    @classmethod
    def from_settings(cls, settings: GitSettings) -> GitForm:
        """The form showing the stored settings, with both credential fields empty."""
        return cls(
            settings.url,
            settings.branch,
            settings.author_name,
            settings.export_format,
            settings.push_enabled,
        )


@dataclass(frozen=True)
class CleanGit:
    """A checked form: the settings to store and what to do with each credential."""

    settings: GitSettings
    deploy_key: str
    token: str
    remove_deploy_key: bool
    remove_token: bool


class GitFormError(Exception):
    """The form has refused values; ``errors`` holds one message per field name."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the messages by field name."""
        super().__init__("the git settings are invalid")
        self.errors = errors


def format_options() -> list[tuple[str, str]]:
    """The export format choices as value and label."""
    return list(FORMAT_LABELS.items())


def _branch_problem(branch: str) -> str | None:
    """Why the text cannot be a branch name, or None when it can."""
    bad = (
        not branch
        or len(branch) > MAX_BRANCH_LENGTH
        or not _BRANCH.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    )
    return "Enter a branch name such as main." if bad else None


def _author_problem(name: str) -> str | None:
    """Why the text cannot be a commit author name, or None when it can."""
    if not name or len(name) > MAX_AUTHOR_LENGTH:
        return f"Enter a name of 1 to {MAX_AUTHOR_LENGTH} characters."
    if not name.isprintable() or "<" in name or ">" in name:
        return "Use printable characters, without < or >."
    return None


def _token_problem(token: str) -> str | None:
    """Why the text cannot be an access token, or None when it can."""
    if len(token) > MAX_TOKEN_LENGTH or not token.isprintable() or any(c.isspace() for c in token):
        return "Use printable characters without spaces, at most 1024."
    return None


def validate_git(form: GitForm) -> CleanGit:
    """The settings the form stands for, or ``GitFormError`` with a message per field.

    An empty address switches git sync off. The address is checked for its transport and for
    anything that does not belong in it (a password, an option, another transport helper).
    """
    errors: dict[str, str] = {}
    url = form.url.strip()
    if url:
        try:
            parse_remote(url)
        except RemoteError as error:
            errors["url"] = str(error)
    branch = form.branch.strip()
    if problem := _branch_problem(branch):
        errors["branch"] = problem
    author = form.author_name.strip()
    if problem := _author_problem(author):
        errors["author_name"] = problem
    if form.export_format not in EXPORT_FORMATS:
        errors["export_format"] = "Choose one of the listed formats."
    key = form.deploy_key
    if key.strip():
        if problem := deploy_key_problem(key):
            errors["deploy_key"] = problem
        elif form.remove_deploy_key:
            errors["deploy_key"] = "Paste a new key or remove the stored one, not both."
    token = form.token.strip()
    if token:
        if problem := _token_problem(token):
            errors["token"] = problem
        elif form.remove_token:
            errors["token"] = "Type a new token or remove the stored one, not both."  # noqa: S105
    if errors:
        raise GitFormError(errors)
    return CleanGit(
        GitSettings(url, branch, author, form.export_format, form.push_enabled),
        normalize_deploy_key(key) if key.strip() else "",
        token,
        form.remove_deploy_key,
        form.remove_token,
    )


async def save_git(
    db: AsyncSession,
    crypto: CryptoService,
    clean: CleanGit,
    *,
    actor: AuditActor,
    locations: GitLocations,
) -> bool:
    """Store the settings and the credential changes, audit them and commit.

    Returns whether anything changed. A new address forgets the old repository's host key,
    watermark and working clone; a new branch or format forgets the watermark; any change
    forgets the failures and the wait, so the next scheduled run tries again.
    """
    changes = await save_section(db, "git", clean.settings, actor=actor)
    names = {change.setting for change in changes}
    secrets_changed = False
    if clean.deploy_key:
        await write_secret(db, crypto, GIT_DEPLOY_KEY_KEY, clean.deploy_key, actor=actor)
        secrets_changed = True
    elif clean.remove_deploy_key:
        secrets_changed |= await clear_secret(db, GIT_DEPLOY_KEY_KEY, actor=actor)
    if clean.token:
        await write_secret(db, crypto, GIT_TOKEN_KEY, clean.token, actor=actor)
        secrets_changed = True
    elif clean.remove_token:
        secrets_changed |= await clear_secret(db, GIT_TOKEN_KEY, actor=actor)
    address_changed = "git.url" in names
    if changes or secrets_changed:
        state = await load_state(db)
        forget_failures(state)
        if address_changed or names & {"git.branch", "git.export_format"}:
            reset_progress(state)
        if address_changed and state.host_key is not None:
            await record_actor_audit(
                db,
                actor,
                action=ACTION_PIN_CLEAR,
                result=AuditResult.SUCCESS,
                target_type=TARGET_TYPE,
                target_name="git",
                details={"reason": "address_changed", "fingerprint": state.host_key_fingerprint},
            )
            clear_host_key(state)
    await db.commit()
    if address_changed:
        await asyncio.to_thread(locations.reset_work)
    return bool(changes) or secrets_changed
