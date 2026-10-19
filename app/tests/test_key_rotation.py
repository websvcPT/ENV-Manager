"""Re-keying the database: every sealed value, every keyed hash, atomicity and the audit entry.

The tests run on the seeded database with every kind of sealed or keyed value added to it (a
secret for each name in ``SECRET_KEYS``, remembered sign-in addresses, git sync progress and real
deploys that leave hashes), so none of them can pass for lack of data.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.crypto import CryptoError, CryptoService, generate_master_key, parse_master_key
from app.models import (
    DeployResult,
    DeployResultStatus,
    DeployStatus,
    DriftState,
    EnvironmentVersion,
    GitSyncState,
    User,
    UserSeenAddress,
)
from app.models.app_setting import KEY_CHECK_SETTING, AppSetting
from app.models.types import utc_now
from app.services import key_rotation, totp
from app.services.app_settings import SECRET_KEYS, secret_context, write_secret
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_types import DeployOutcome
from app.services.git_state import load_state
from app.services.key_check import ensure_key_check
from app.services.key_rotation import ACTION_ROTATE, RotationError, rotate_master_key
from app.services.seen_addresses import note_sign_in_address
from app.services.versions import create_version
from app.tests.deploy_helpers import Harness, audit_of
from app.tests.key_helpers import dump_database, sealed_values, text_values

KEYED_COLUMNS = {
    ("environment_versions", "content_hash"),
    ("deploy_results", "target_hash_before"),
    ("deploy_results", "written_hash"),
    ("user_seen_addresses", "address_hash"),
    ("git_sync_state", "watermark"),
}
DRIFTED_TEXT = "WORKERS=99\n"


@dataclass
class World:
    """The database before a rotation: what was in it, and the two keys."""

    harness: Harness
    old: CryptoService
    new: CryptoService
    old_encoded: str
    new_encoded: str
    path: Path
    secrets: dict[str, str]
    versions: dict[int, str]
    sealed_before: set[str]
    keyed_before: set[str]

    async def rotate(self) -> key_rotation.RekeyReport:
        """Rotate to the new key on the harness session."""
        return await rotate_master_key(
            self.harness.db,
            old=self.old,
            new=self.new,
            actor=AuditActor.cli(),
            backup="envmanager-test-rotation.db",
        )


def tuning_target(h: Harness) -> Path:
    """The target file of the seeded tuning environment."""
    return h.roots["staging"] / "environments" / h.seed.figures.tuning.name


async def deploy_tuning(h: Harness, *, force: bool = False) -> DeployOutcome:
    """Deploy the tuning file of the staging web service."""
    return await h.deploy(
        h.seed.services.staging_web.id, environments=[h.seed.figures.tuning], force=force
    )


async def build_world(h: Harness, master_key: str) -> World:
    """Add one of every kind of sealed or keyed value, then describe the database."""
    db = h.db
    actor = AuditActor.cli()
    await ensure_key_check(db, h.crypto)
    secrets = {name: f"secret-value-for-{name}" for name in sorted(SECRET_KEYS)}
    for name, value in secrets.items():
        await write_secret(db, h.crypto, name, value, actor=actor)
    for user in (h.seed.users.admin, h.seed.users.viewer):
        await note_sign_in_address(db, h.crypto, user.id, "203.0.113.7", utc_now())
    state = await load_state(db)
    state.watermark = h.crypto.keyed_hash("git-mark", "marker")
    state.failures = 2
    await db.commit()

    tuning = h.seed.figures.tuning
    assert (await deploy_tuning(h)).status is DeployStatus.SUCCESS
    await create_version(db, h.crypto, tuning.id, "WORKERS=8\n", author=h.seed.users.admin)
    await db.commit()
    assert (await deploy_tuning(h)).status is DeployStatus.SUCCESS
    tuning_target(h).write_text(DRIFTED_TEXT, encoding="utf-8")
    assert (await deploy_tuning(h, force=True)).status is DeployStatus.SUCCESS

    path = Path(str(h.database.url.database))
    versions = {
        version.id: h.crypto.decrypt_text(version.content_encrypted)
        for version in (await db.execute(select(EnvironmentVersion))).scalars()
    }
    cells = text_values(path)
    keyed = {value for table, column, value in cells if (table, column) in KEYED_COLUMNS}
    new_encoded = generate_master_key()
    return World(
        harness=h,
        old=h.crypto,
        new=CryptoService(parse_master_key(new_encoded)),
        old_encoded=master_key,
        new_encoded=new_encoded,
        path=path,
        secrets=secrets,
        versions=versions,
        sealed_before={value for _, _, value in sealed_values(path)},
        keyed_before=keyed,
    )


@pytest.fixture
async def world(harness: Harness, master_key: str) -> World:
    """The deploy harness on a database that holds every kind of sealed and keyed value."""
    return await build_world(harness, master_key)


async def test_the_world_holds_every_kind_of_value_a_rotation_must_move(world: World) -> None:
    """A guard for the tests below: each category exists, so none of them is vacuous."""
    h = world.harness
    db = h.db
    assert len(world.versions) >= 12
    assert len(world.secrets) == len(SECRET_KEYS) >= 4
    assert (await db.execute(select(User).where(User.totp_secret.is_not(None)))).scalars().all()
    assert (await db.execute(select(UserSeenAddress))).scalars().all()
    results = (await db.execute(select(DeployResult))).scalars().all()
    assert any(result.written_hash for result in results)
    assert any(result.target_hash_before for result in results)
    state = await load_state(db)
    assert state.watermark and state.failures == 2
    kinds = {table for table, column in KEYED_COLUMNS}
    present = {table for table, column, _ in text_values(world.path) if table in kinds}
    assert present == kinds


async def test_every_sealed_value_opens_with_the_new_key_to_the_same_plaintext(
    world: World,
) -> None:
    h = world.harness
    totp_before = {
        user.id: totp.read_secret(user, world.old)
        for user in (await h.db.execute(select(User))).scalars()
        if user.totp_secret
    }
    assert totp_before and all(totp_before.values())
    await world.rotate()
    h.db.expire_all()

    versions = (await h.db.execute(select(EnvironmentVersion))).scalars().all()
    assert {version.id for version in versions} == set(world.versions)
    for version in versions:
        assert world.new.decrypt_text(version.content_encrypted) == world.versions[version.id]
    for user in (await h.db.execute(select(User))).scalars():
        if user.id in totp_before:
            assert totp.read_secret(user, world.new) == totp_before[user.id]
            assert totp.read_secret(user, world.old) is None
    for name, value in world.secrets.items():
        row = await h.db.get(AppSetting, name)
        assert row is not None
        assert world.new.decrypt_text(row.value, secret_context(name)) == value
    check = await h.db.get(AppSetting, KEY_CHECK_SETTING)
    assert check is not None
    world.new.verify_key_check(check.value)


async def test_every_content_hash_is_recomputed_and_deploy_hashes_follow_their_versions(
    world: World,
) -> None:
    h = world.harness
    report = await world.rotate()
    h.db.expire_all()

    versions = {
        version.id: version
        for version in (await h.db.execute(select(EnvironmentVersion))).scalars()
    }
    for version_id, text in world.versions.items():
        assert versions[version_id].content_hash == world.new.content_hash(text)
        assert versions[version_id].content_hash != world.old.content_hash(text)
    results = (await h.db.execute(select(DeployResult).order_by(DeployResult.id))).scalars().all()
    written = [result for result in results if result.written_hash]
    assert written
    for result in written:
        assert result.version_id is not None
        assert result.written_hash == versions[result.version_id].content_hash
    assert results[-1].status is DeployResultStatus.WRITTEN
    assert results[-1].target_hash_before is None
    assert results[-1].written_hash == versions[results[-1].version_id or 0].content_hash
    followed = [result for result in results if result.target_hash_before]
    assert followed
    assert {result.target_hash_before for result in followed} <= {
        version.content_hash for version in versions.values()
    }
    assert report.deploy_hashes_dropped == 1
    assert report.versions == len(world.versions)


async def test_nothing_sealed_or_keyed_under_the_old_key_is_left_in_any_table(
    world: World,
) -> None:
    await world.rotate()
    cells = text_values(world.path)
    sealed_after = {value for _, _, value in sealed_values(world.path)}
    assert sealed_after, "the scan must find sealed values to mean anything"
    assert not sealed_after & world.sealed_before
    values_after = {value for _, _, value in cells}
    assert world.keyed_before
    assert not values_after & world.keyed_before

    contexts = [b"", *(secret_context(name) for name in SECRET_KEYS)]
    contexts += [totp.secret_context(user_id) for user_id in range(1, 40)]
    contexts.append(b"key-check")
    for table, column, value in sealed_values(world.path):
        opened = []
        for context in contexts:
            try:
                world.new.decrypt(value, context)
            except CryptoError:
                continue
            opened.append(context)
        assert opened, f"{table}.{column} holds a value the new key does not open"


async def test_the_old_key_opens_nothing_afterwards(world: World) -> None:
    h = world.harness
    await world.rotate()
    h.db.expire_all()
    check = await h.db.get(AppSetting, KEY_CHECK_SETTING)
    assert check is not None
    with pytest.raises(Exception, match="does not match"):
        world.old.verify_key_check(check.value)
    version = (await h.db.execute(select(EnvironmentVersion).limit(1))).scalar_one()
    with pytest.raises(Exception, match="authentication"):
        world.old.decrypt_text(version.content_encrypted)


async def test_drift_is_still_detected_and_the_baseline_still_matches_under_the_new_key(
    world: World,
) -> None:
    h = world.harness
    await world.rotate()
    tuning = h.seed.figures.tuning
    h.engine = DeployEngine(world.new, h.lock_dir, hook_runner=h.runner, notifier=h.notifier)
    target = tuning_target(h)

    assert target.read_text(encoding="utf-8") == "WORKERS=8\n"
    await create_version(h.db, world.new, tuning.id, "WORKERS=16\n", author=h.seed.users.admin)
    await h.db.commit()
    written = await deploy_tuning(h)
    assert written.environments[0].status is DeployResultStatus.WRITTEN
    assert written.environments[0].drift is DriftState.IN_SYNC
    assert target.read_text(encoding="utf-8") == "WORKERS=16\n"

    target.write_text("WORKERS=77\n", encoding="utf-8")
    await create_version(h.db, world.new, tuning.id, "WORKERS=32\n", author=h.seed.users.admin)
    await h.db.commit()
    halted = await deploy_tuning(h)
    assert halted.environments[0].status is DeployResultStatus.HALTED
    assert halted.environments[0].drift is DriftState.DRIFT
    assert target.read_text(encoding="utf-8") == "WORKERS=77\n"


async def test_remembered_sign_in_addresses_are_cleared_and_git_progress_is_reset(
    world: World,
) -> None:
    h = world.harness
    report = await world.rotate()
    h.db.expire_all()
    assert report.seen_addresses_cleared == 2 and report.git_progress_reset
    assert (await h.db.execute(select(UserSeenAddress))).scalars().all() == []
    state = await h.db.get(GitSyncState, 1)
    assert state is not None
    assert (state.watermark, state.failures, state.last_success_at) == (None, 0, None)


async def test_the_audit_entry_has_counts_and_the_backup_and_never_key_material(
    world: World,
) -> None:
    h = world.harness
    await world.rotate()
    (entry,) = await audit_of(h.db, ACTION_ROTATE)
    assert entry.actor_name == "cli" and entry.target_type == "key"
    assert entry.details["backup"] == "envmanager-test-rotation.db"
    assert entry.details["versions"] == len(world.versions)
    assert entry.details["setting_secrets"] == len(SECRET_KEYS)
    assert entry.details["git_progress_reset"] is True
    text = json.dumps(entry.details) + str(entry.target_name)
    for needle in (world.old_encoded, world.new_encoded, *world.secrets.values()):
        assert needle not in text


async def test_a_failure_in_the_middle_leaves_the_database_exactly_as_it_was(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = world.harness
    before = dump_database(world.path)
    original = key_rotation._rekey_settings

    async def fail_after_the_versions_moved(*args: object) -> int:
        """Run after versions and second factor secrets were already rewritten."""
        raise RuntimeError("injected failure")

    monkeypatch.setattr(key_rotation, "_rekey_settings", fail_after_the_versions_moved)
    with pytest.raises(RuntimeError, match="injected"):
        await world.rotate()
    assert dump_database(world.path) == before
    h.db.expire_all()
    monkeypatch.setattr(key_rotation, "_rekey_settings", original)
    await world.rotate()
    assert dump_database(world.path) != before


async def test_a_value_the_old_key_cannot_open_stops_the_rotation_and_changes_nothing(
    world: World,
) -> None:
    h = world.harness
    version = (await h.db.execute(select(EnvironmentVersion).limit(1))).scalar_one()
    version.content_encrypted = world.new.encrypt("sealed with another key")
    await h.db.commit()
    before = dump_database(world.path)
    with pytest.raises(RotationError, match=f"version {version.id} cannot be opened"):
        await world.rotate()
    assert dump_database(world.path) == before


async def test_a_secret_setting_sealed_under_another_name_stops_the_rotation(
    world: World,
) -> None:
    h = world.harness
    names = sorted(SECRET_KEYS)
    first = await h.db.get(AppSetting, names[0])
    second = await h.db.get(AppSetting, names[1])
    assert first is not None and second is not None
    first.value = second.value
    await h.db.commit()
    before = dump_database(world.path)
    with pytest.raises(RotationError, match=f"the setting {names[0]} cannot be opened"):
        await world.rotate()
    assert dump_database(world.path) == before


async def test_a_database_without_a_key_check_record_is_refused(world: World) -> None:
    h = world.harness
    row = await h.db.get(AppSetting, KEY_CHECK_SETTING)
    assert row is not None
    await h.db.delete(row)
    await h.db.commit()
    before = dump_database(world.path)
    with pytest.raises(RotationError, match="no key check record"):
        await world.rotate()
    assert dump_database(world.path) == before


async def test_a_second_rotation_back_to_the_first_key_round_trips(world: World) -> None:
    h = world.harness
    await world.rotate()
    h.db.expire_all()
    await rotate_master_key(h.db, old=world.new, new=world.old, actor=AuditActor.cli(), backup=None)
    h.db.expire_all()
    for version in (await h.db.execute(select(EnvironmentVersion))).scalars():
        assert world.old.decrypt_text(version.content_encrypted) == world.versions[version.id]
        assert version.content_hash == world.old.content_hash(world.versions[version.id])
