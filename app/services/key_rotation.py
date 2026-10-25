"""Re-keying a database: every ciphertext and keyed hash moves from one master key to another.

``rekey_database`` does the work on a session and never commits, so the caller decides what the
transaction is: the key rotation command runs it on the live database in one transaction together
with its audit entry, and a restore of a backup made under an older key runs it on the private
working copy before that copy is switched in.

What is sealed or keyed by the master key is listed here and nowhere else:

* the content of every environment version (sealed) and its content hash (keyed),
* the second factor secret of every user (sealed, bound to the user),
* every secret setting named in ``SECRET_KEYS`` (sealed, bound to the setting name) and the key
  check record,
* the token of every Docker registry login (sealed, bound to the registry),
* the hashes of deploy results (keyed): the hash a deploy wrote is the content hash of its
  version, so it follows the version; the hash of the file found before a deploy cannot be
  derived from anything stored, so it follows a version when the file held one and is dropped
  otherwise (nothing reads it),
* the client addresses a user signed in from (keyed, the addresses themselves are not stored):
  cleared, so each user learns their next address silently,
* the watermark of git sync (keyed): progress is reset, so the next run pushes the export again
  under the new key.

A test scans every table of a populated database for a value sealed under the old key, so a
column added later that is not handled here fails there.

Messages name ids and counts, never a value or a key.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.crypto import CryptoError, CryptoService
from app.models import (
    SYNC_STATE_ID,
    AppSetting,
    AuditResult,
    DeployResult,
    DockerRegistry,
    EnvironmentVersion,
    GitSyncState,
    User,
    UserSeenAddress,
)
from app.models.app_setting import KEY_CHECK_SETTING
from app.services import totp
from app.services.app_settings import SECRET_KEYS, secret_context
from app.services.audit import AuditActor, record_actor_audit
from app.services.git_state import reset_progress
from app.services.registries import registry_context

ACTION_ROTATE = "key.rotate"
TARGET_TYPE = "key"
TARGET_NAME = "master key"
BATCH_SIZE = 200


class RotationError(Exception):
    """The data cannot be re-keyed. The message is safe to show: it names ids, never values."""


@dataclass(frozen=True)
class RekeyReport:
    """What a re-key changed, as counts."""

    versions: int
    totp_secrets: int
    setting_secrets: int
    registry_tokens: int
    deploy_hashes_recomputed: int
    deploy_hashes_dropped: int
    seen_addresses_cleared: int
    git_progress_reset: bool


def _open(old: CryptoService, sealed: str, context: bytes, what: str) -> bytes:
    """The plaintext of a sealed value, or a ``RotationError`` that names only what it was."""
    try:
        return old.decrypt(sealed, context)
    except CryptoError as error:
        raise RotationError(f"{what} cannot be opened with the old key") from error


async def _batches(
    db: AsyncSession, columns: Sequence[InstrumentedAttribute[Any]], *conditions: Any
) -> AsyncIterator[list[Any]]:
    """Rows of a table in batches by ascending id, the id being the first column."""
    key = columns[0]
    last = 0
    while True:
        statement = select(*columns).where(key > last, *conditions).order_by(key).limit(BATCH_SIZE)
        rows = (await db.execute(statement)).all()
        if not rows:
            return
        yield list(rows)
        last = rows[-1][0]


async def _rekey_versions(
    db: AsyncSession, old: CryptoService, new: CryptoService
) -> tuple[int, dict[str, str]]:
    """Reseal every version and recompute its hash. Returns the count and old hash to new hash."""
    hashes: dict[str, str] = {}
    count = 0
    columns = (
        EnvironmentVersion.id,
        EnvironmentVersion.content_encrypted,
        EnvironmentVersion.content_hash,
    )
    async for rows in _batches(db, columns):
        changes = []
        for row_id, sealed, stored_hash in rows:
            plain = _open(old, sealed, b"", f"version {row_id}")
            digest = new.content_hash(plain)
            hashes[stored_hash] = digest
            hashes[old.content_hash(plain)] = digest
            changes.append(
                {"id": row_id, "content_encrypted": new.encrypt(plain), "content_hash": digest}
            )
        await db.execute(update(EnvironmentVersion), changes)
        count += len(rows)
    return count, hashes


async def _users_with_secret(db: AsyncSession) -> list[tuple[int, str]]:
    """The id and sealed second factor secret of every user that has one."""
    rows = (
        await db.execute(
            select(User.id, User.totp_secret).where(
                User.totp_secret.is_not(None), User.totp_secret != ""
            )
        )
    ).all()
    return [(user_id, sealed) for user_id, sealed in rows if sealed]


async def _sealed_settings(db: AsyncSession) -> dict[str, str]:
    """The stored value of every secret setting and of the key check record, by name."""
    names = [*SECRET_KEYS, KEY_CHECK_SETTING]
    rows = (
        await db.execute(select(AppSetting.key, AppSetting.value).where(AppSetting.key.in_(names)))
    ).all()
    return dict(rows)


async def _rekey_totp_secrets(db: AsyncSession, old: CryptoService, new: CryptoService) -> int:
    """Reseal the second factor secret of every user that has one."""
    changes = []
    for user_id, sealed in await _users_with_secret(db):
        context = totp.secret_context(user_id)
        plain = _open(old, sealed, context, f"the second factor secret of user {user_id}")
        changes.append({"id": user_id, "totp_secret": new.encrypt(plain, context)})
    if changes:
        await db.execute(update(User), changes)
    return len(changes)


async def _rekey_settings(db: AsyncSession, old: CryptoService, new: CryptoService) -> int:
    """Reseal every secret setting and the key check record. Returns the secrets resealed."""
    values = await _sealed_settings(db)
    check = values.pop(KEY_CHECK_SETTING, None)
    if check is None:
        raise RotationError("the database has no key check record")
    try:
        old.verify_key_check(check)
    except CryptoError as error:
        raise RotationError("the key check record does not open with the old key") from error
    changes = []
    for name, sealed in values.items():
        context = secret_context(name)
        plain = _open(old, sealed, context, f"the setting {name}")
        changes.append({"key": name, "value": new.encrypt(plain, context)})
    secrets_resealed = len(changes)
    changes.append({"key": KEY_CHECK_SETTING, "value": new.make_key_check()})
    await db.execute(update(AppSetting), changes)
    return secrets_resealed


async def _sealed_registries(db: AsyncSession) -> list[tuple[int, str]]:
    """The id and sealed token of every Docker registry login."""
    rows = (
        await db.execute(
            select(DockerRegistry.id, DockerRegistry.token_encrypted).order_by(DockerRegistry.id)
        )
    ).all()
    return [(registry_id, sealed) for registry_id, sealed in rows]


async def _rekey_registries(db: AsyncSession, old: CryptoService, new: CryptoService) -> int:
    """Reseal the token of every registry login. The time of the last edit is left as it was."""
    count = 0
    for registry_id, sealed in await _sealed_registries(db):
        context = registry_context(registry_id)
        plain = _open(old, sealed, context, f"the token of registry {registry_id}")
        resealed = new.encrypt(plain, context)
        await db.execute(
            update(DockerRegistry)
            .where(DockerRegistry.id == registry_id)
            .values(token_encrypted=resealed, updated_at=DockerRegistry.updated_at)
        )
        count += 1
    return count


def _moved(value: str | None, hashes: dict[str, str]) -> str | None:
    """The new hash for an old one, or None when the old one maps to no known version."""
    return hashes.get(value) if value is not None else None


async def _rekey_deploy_hashes(db: AsyncSession, hashes: dict[str, str]) -> tuple[int, int]:
    """Move the hashes of deploy results to the new key. Returns recomputed and dropped counts."""
    recomputed = dropped = 0
    has_hash = or_(
        DeployResult.target_hash_before.is_not(None), DeployResult.written_hash.is_not(None)
    )
    columns = (DeployResult.id, DeployResult.target_hash_before, DeployResult.written_hash)
    async for rows in _batches(db, columns, has_hash):
        changes = []
        for row_id, before, written in rows:
            moved = {
                "target_hash_before": _moved(before, hashes),
                "written_hash": _moved(written, hashes),
            }
            for old_value, new_value in (
                (before, moved["target_hash_before"]),
                (written, moved["written_hash"]),
            ):
                if old_value is None:
                    continue
                if new_value is None:
                    dropped += 1
                else:
                    recomputed += 1
            changes.append({"id": row_id, **moved})
        await db.execute(update(DeployResult), changes)
    return recomputed, dropped


async def _forget_signed_in_addresses(db: AsyncSession) -> int:
    """Delete the keyed hashes of client addresses, which cannot be recomputed."""
    result = await db.execute(delete(UserSeenAddress))
    return int(result.rowcount)  # type: ignore[attr-defined]


async def _reset_git_progress(db: AsyncSession) -> bool:
    """Make the next git push send the whole export again, if git sync has ever run."""
    state = await db.get(GitSyncState, SYNC_STATE_ID)
    if state is None:
        return False
    reset_progress(state)
    await db.flush()
    return True


async def _verify(db: AsyncSession, new: CryptoService) -> None:
    """Open everything that was resealed with the new key, before anything is committed."""
    columns = (
        EnvironmentVersion.id,
        EnvironmentVersion.content_encrypted,
        EnvironmentVersion.content_hash,
    )
    async for rows in _batches(db, columns):
        for row_id, sealed, stored_hash in rows:
            plain = _open(new, sealed, b"", f"version {row_id} after the re-key")
            if new.content_hash(plain) != stored_hash:
                raise RotationError(f"the hash of version {row_id} does not match after the re-key")
    for user_id, sealed in await _users_with_secret(db):
        _open(new, sealed, totp.secret_context(user_id), f"the second factor of user {user_id}")
    for registry_id, sealed in await _sealed_registries(db):
        _open(
            new,
            sealed,
            registry_context(registry_id),
            f"the token of registry {registry_id} after the re-key",
        )
    for name, sealed in (await _sealed_settings(db)).items():
        if name != KEY_CHECK_SETTING:
            _open(new, sealed, secret_context(name), f"the setting {name} after the re-key")
            continue
        try:
            new.verify_key_check(sealed)
        except CryptoError as error:
            raise RotationError("the key check record does not open after the re-key") from error


async def rekey_database(db: AsyncSession, old: CryptoService, new: CryptoService) -> RekeyReport:
    """Move everything the master key protects from ``old`` to ``new``, without committing.

    Raises ``RotationError`` when something cannot be opened with the old key; the caller then
    rolls the transaction back. The result is checked by opening every resealed value with the
    new key before it returns.
    """
    versions, hashes = await _rekey_versions(db, old, new)
    totp_secrets = await _rekey_totp_secrets(db, old, new)
    setting_secrets = await _rekey_settings(db, old, new)
    registry_tokens = await _rekey_registries(db, old, new)
    recomputed, dropped = await _rekey_deploy_hashes(db, hashes)
    cleared = await _forget_signed_in_addresses(db)
    reset = await _reset_git_progress(db)
    await _verify(db, new)
    return RekeyReport(
        versions=versions,
        totp_secrets=totp_secrets,
        setting_secrets=setting_secrets,
        registry_tokens=registry_tokens,
        deploy_hashes_recomputed=recomputed,
        deploy_hashes_dropped=dropped,
        seen_addresses_cleared=cleared,
        git_progress_reset=reset,
    )


async def rotate_master_key(
    db: AsyncSession,
    *,
    old: CryptoService,
    new: CryptoService,
    actor: AuditActor,
    backup: str | None,
) -> RekeyReport:
    """Re-key the live database and audit it, in one transaction.

    The write lock is taken before anything is read, so nothing can change between reading a
    value and replacing it. A failure of any kind rolls everything back and the database is as it
    was. The audit entry holds counts and the name of the backup taken first, never key material.
    """
    await db.execute(text("BEGIN IMMEDIATE"))
    try:
        report = await rekey_database(db, old, new)
        await record_actor_audit(
            db,
            actor,
            action=ACTION_ROTATE,
            result=AuditResult.SUCCESS,
            target_type=TARGET_TYPE,
            target_name=TARGET_NAME,
            details={**asdict(report), "backup": backup},
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return report
