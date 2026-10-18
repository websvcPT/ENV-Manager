"""The drift view service: the comparison, its states, audited reveals, import and overwrite.

The seeded staging deployment is moved onto a real temporary folder, so the target is read through
the confined reader exactly as the application reads it, and drift is judged against what a real
deploy of the engine recorded.
"""

import stat
from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app.models import (
    AllowedPath,
    AuditEntry,
    AuditResult,
    DeployResultStatus,
    DriftPolicy,
    Environment,
    EnvironmentVersion,
)
from app.services.audit import AuditActor
from app.services.deploy_lock import hold_deploy_lock
from app.services.drift import (
    IMPORT_COMMENT,
    Comparison,
    DriftChangedError,
    DriftError,
    DriftKind,
    DriftView,
    OverwriteResult,
    Presence,
    RevealedDrift,
    compare_columns,
    import_target,
    load_drift_view,
    overwrite_target,
    reveal_drift,
)
from app.services.editor import STALE_MESSAGE, EditorHeader, load_editor_header
from app.services.version_history import WHOLE_FILE_KEY, DiffChange
from app.services.versions import create_version
from app.tests.deploy_helpers import (
    Harness,
    audit_of,
    files_in,
    set_deployment,
    version_text,
)

MANAGED = "A=1\nB=2\nSHARED=same\n"
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


def drift_texts(target: str | None, deployed: str | None, managed: str) -> Comparison:
    """The comparison of three texts."""
    return compare_columns(target, deployed, managed)


def changes(comparison: Comparison) -> dict[str, tuple[DiffChange | None, DiffChange | None]]:
    """The disk and managed change of each listed row by key."""
    return {row.key: (row.disk_change, row.managed_change) for row in comparison.rows}


def test_identical_columns_have_no_rows_and_count_every_key_as_unchanged() -> None:
    comparison = drift_texts(MANAGED, MANAGED, MANAGED)
    assert comparison.rows == () and comparison.unchanged == 3
    assert not comparison.whole_file and not comparison.text_only


def test_keys_differ_per_column_and_the_target_changes_come_first() -> None:
    deployed = "MAX_CONNECTIONS=150\nLEGACY_AUTH=old\nSAME=1\n"
    managed = "MAX_CONNECTIONS=200\nTLS_MIN_VERSION=1.2\nSAME=1\n"
    target = "MAX_CONNECTIONS=250\nLEGACY_AUTH=old\nSAME=1\n"
    comparison = drift_texts(target, deployed, managed)

    assert [row.key for row in comparison.rows] == [
        "MAX_CONNECTIONS",
        "TLS_MIN_VERSION",
        "LEGACY_AUTH",
    ]
    assert changes(comparison) == {
        "MAX_CONNECTIONS": (DiffChange.CHANGED, DiffChange.CHANGED),
        "TLS_MIN_VERSION": (None, DiffChange.ADDED),
        "LEGACY_AUTH": (None, DiffChange.REMOVED),
    }
    assert comparison.unchanged == 1
    tls = comparison.rows[1]
    assert (tls.target, tls.deployed, tls.managed) == (
        Presence.NOT_SET,
        Presence.NOT_SET,
        Presence.SET,
    )
    legacy = comparison.rows[2]
    assert (legacy.target, legacy.deployed, legacy.managed) == (
        Presence.SET,
        Presence.SET,
        Presence.NOT_SET,
    )
    assert [row.index for row in comparison.rows] == [0, 1, 2]


def test_a_target_is_compared_with_the_deployed_version_not_the_managed_one() -> None:
    comparison = drift_texts("A=1\nNEW=on\n", "A=1\n", "A=1\nB=2\n")
    assert changes(comparison) == {
        "NEW": (DiffChange.ADDED, None),
        "B": (None, DiffChange.ADDED),
    }


def test_nothing_deployed_compares_the_target_with_the_managed_version() -> None:
    comparison = drift_texts("A=9\nC=3\n", None, "A=1\nB=2\n")
    assert changes(comparison) == {
        "A": (DiffChange.CHANGED, None),
        "C": (DiffChange.ADDED, None),
        "B": (DiffChange.REMOVED, None),
    }
    assert all(row.deployed is Presence.UNAVAILABLE for row in comparison.rows)


def test_a_missing_target_leaves_only_what_managed_changed() -> None:
    comparison = drift_texts(None, "A=1\n", "A=2\nB=1\n")
    assert changes(comparison) == {
        "A": (None, DiffChange.CHANGED),
        "B": (None, DiffChange.ADDED),
    }
    assert all(row.target is Presence.UNAVAILABLE for row in comparison.rows)


def test_a_single_column_has_nothing_to_compare_and_counts_no_unchanged_keys() -> None:
    alone = drift_texts(None, None, "A=1\nB=2\n")
    assert alone.columns == 1 and alone.rows == () and alone.unchanged == 0
    assert not alone.text_only and not alone.whole_file
    assert drift_texts(None, None, CERTIFICATE).rows == ()
    pair = drift_texts("A=1\nB=2\n", None, "A=1\nB=2\n")
    assert pair.columns == 2 and pair.unchanged == 2


def test_the_same_variables_in_another_order_or_with_comments_are_no_rows_but_flagged() -> None:
    comparison = drift_texts("# edited\nB=2\nA=1\n", "A=1\nB=2\n", "A=1\nB=2\n")
    assert comparison.rows == () and comparison.text_only and comparison.unchanged == 2


def test_a_column_that_is_not_env_format_compares_as_one_file_content_row() -> None:
    comparison = drift_texts(CERTIFICATE + "changed\n", CERTIFICATE, CERTIFICATE)
    assert comparison.whole_file and comparison.unchanged == 0
    (row,) = comparison.rows
    assert row.key == WHOLE_FILE_KEY
    assert (row.disk_change, row.managed_change) == (DiffChange.CHANGED, None)
    assert drift_texts(CERTIFICATE, CERTIFICATE, CERTIFICATE).rows == ()


def test_a_free_text_target_never_lists_its_lines_as_keys() -> None:
    comparison = drift_texts("just some words\nnot a variable\n", "A=1\n", "A=1\n")
    assert comparison.whole_file
    assert [row.key for row in comparison.rows] == [WHOLE_FILE_KEY]


def test_a_row_holds_no_value_field() -> None:
    comparison = drift_texts("A=secret-target\n", "A=secret-deployed\n", "A=secret-managed\n")
    flat = repr(comparison)
    assert "secret" not in flat
    assert {field.name for field in fields(comparison.rows[0])} == {
        "index",
        "key",
        "target",
        "deployed",
        "managed",
        "disk_change",
        "managed_change",
    }


class Staged:
    """A staging file with a managed version, its target path and the actors that use it."""

    def __init__(self, h: Harness, environment: Environment, header: EditorHeader) -> None:
        """Hold the harness, the environment and the header the services take."""
        self.h = h
        self.environment = environment
        self.header = header

    @property
    def target(self) -> Path:
        """The file on disk."""
        return self.h.roots["staging"] / self.environment.target_path

    async def view(self) -> DriftView:
        """The drift view now."""
        view = await load_drift_view(self.h.db, self.h.crypto, self.header)
        assert view is not None
        return view

    async def deploy(self, *, force: bool = False) -> DeployResultStatus:
        """Deploy the file and return its status."""
        outcome = await self.h.deploy(
            self.h.seed.services.staging_web.id,
            environments=[self.environment],
            force=force,
        )
        return outcome.environments[0].status

    async def versions(self) -> int:
        """How many versions the file has."""
        return (
            await self.h.db.scalar(
                select(func.count())
                .select_from(EnvironmentVersion)
                .where(EnvironmentVersion.environment_id == self.environment.id)
            )
            or 0
        )


@pytest.fixture
async def staged(harness: Harness) -> Staged:
    """The staging web file with version MANAGED as its newest version."""
    h = harness
    web = h.seed.envs.staging_web
    await create_version(h.db, h.crypto, web.id, MANAGED, author=h.seed.users.admin)
    await h.db.commit()
    header = await load_editor_header(h.db, await h.scope_of("superadmin"), web.id)
    assert header is not None
    return Staged(h, web, header)


async def test_a_file_that_was_never_deployed_with_content_on_disk_reads_never_deployed(
    staged: Staged,
) -> None:
    staged.target.write_text("A=1\nB=changed\n", encoding="utf-8")
    view = await staged.view()
    assert view.kind is DriftKind.NEVER_DEPLOYED
    assert view.deployed is None and view.can_import and view.can_overwrite
    assert view.target.readable and view.target_hash is not None
    assert changes(view.comparison) == {
        "B": (DiffChange.CHANGED, None),
        "SHARED": (DiffChange.REMOVED, None),
    }


async def test_a_never_deployed_target_that_is_empty_or_identical_is_no_drift(
    staged: Staged,
) -> None:
    staged.target.write_text("  \n", encoding="utf-8")
    view = await staged.view()
    assert view.kind is DriftKind.NO_DRIFT
    assert view.message == "The target file is empty. The first deploy writes the managed version."
    assert not view.can_import and not view.can_overwrite
    staged.target.write_text(MANAGED, encoding="utf-8")
    view = await staged.view()
    assert view.kind is DriftKind.NO_DRIFT
    assert view.message == "The target file matches the managed version."


async def test_a_deployed_target_is_no_drift_and_one_edited_since_is_drift(
    staged: Staged,
) -> None:
    assert await staged.deploy() is DeployResultStatus.WRITTEN
    view = await staged.view()
    assert view.kind is DriftKind.NO_DRIFT and view.comparison.rows == ()
    assert view.deployed is not None and view.deployed.number == view.managed.number
    assert not view.can_import and not view.can_overwrite

    staged.target.write_text("A=9\nC=3\nSHARED=same\n", encoding="utf-8")
    view = await staged.view()
    assert view.kind is DriftKind.DRIFT
    assert view.message.startswith("The target file changed outside the app")
    assert view.can_import and view.can_overwrite
    assert changes(view.comparison) == {
        "A": (DiffChange.CHANGED, None),
        "C": (DiffChange.ADDED, None),
        "B": (DiffChange.REMOVED, None),
    }
    assert view.comparison.unchanged == 1


async def test_a_newer_managed_version_waits_for_a_deploy_without_drift(
    staged: Staged,
) -> None:
    assert await staged.deploy() is DeployResultStatus.WRITTEN
    await create_version(
        staged.h.db,
        staged.h.crypto,
        staged.environment.id,
        MANAGED + "NEWER=1\n",
        author=staged.h.seed.users.admin,
    )
    await staged.h.db.commit()
    view = await staged.view()
    assert view.kind is DriftKind.NO_DRIFT
    assert view.message == (
        "The target file matches the last deployed version. "
        f"Version {view.managed.number} is newer and waits for a deploy."
    )
    assert changes(view.comparison) == {"NEWER": (None, DiffChange.ADDED)}
    assert view.deployed is not None and view.deployed.number == view.managed.number - 1


async def test_a_missing_target_or_folder_reads_missing_without_a_path(staged: Staged) -> None:
    root = str(staged.h.roots["staging"])
    view = await staged.view()
    assert view.kind is DriftKind.MISSING and view.message == "The target file does not exist."
    assert not view.can_import and not view.can_overwrite and not view.target.readable
    staged.target.parent.rmdir()
    view = await staged.view()
    assert view.kind is DriftKind.MISSING and view.message == "The target folder does not exist."
    assert root not in repr(view)


async def test_a_target_that_cannot_be_read_is_unreadable_with_a_fixed_sentence(
    staged: Staged, tmp_path: Path
) -> None:
    root = str(staged.h.roots["staging"])
    outside = tmp_path / "elsewhere"
    outside.write_text("A=1\n", encoding="utf-8")
    staged.target.symlink_to(outside)
    view = await staged.view()
    assert view.kind is DriftKind.UNREADABLE
    assert view.message == "The target is a symbolic link and is not replaced."
    assert not view.can_import and not view.can_overwrite

    staged.target.unlink()
    staged.target.mkdir()
    view = await staged.view()
    assert view.kind is DriftKind.UNREADABLE and view.message == "The target is not a regular file."
    staged.target.rmdir()

    staged.target.write_bytes(b"A=\xff\xfe\n")
    view = await staged.view()
    assert view.kind is DriftKind.UNREADABLE
    assert view.message == "The target file is not valid UTF-8 text, so it cannot be compared."
    assert view.target_hash is not None and not view.can_import

    staged.target.write_bytes(b"A=1\n" + b"x" * (4 * 1024 * 1024))
    view = await staged.view()
    assert view.kind is DriftKind.UNREADABLE
    assert view.message == "The target file is too large to compare."
    assert root not in repr(view) and str(outside) not in repr(view)


async def test_a_deployment_outside_the_allowed_paths_reads_outside_without_a_path(
    staged: Staged,
) -> None:
    root = str(staged.h.roots["staging"])
    staged.target.write_text("A=1\n", encoding="utf-8")
    await staged.h.db.execute(delete(AllowedPath))
    await staged.h.db.commit()
    view = await staged.view()
    assert view.kind is DriftKind.OUTSIDE
    assert view.message == "The deployment is outside the allowed paths."
    assert not view.target.readable and view.target_hash is None
    assert not view.can_import and not view.can_overwrite
    assert root not in repr(view)


async def test_a_folder_symlinked_out_of_the_deployment_is_never_read(
    staged: Staged, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / staged.environment.name).write_text("LEAK=outside-value\n", encoding="utf-8")
    staged.target.parent.rmdir()
    staged.target.parent.symlink_to(elsewhere)
    view = await staged.view()
    assert view.kind is DriftKind.OUTSIDE and not view.target.readable
    assert "outside-value" not in repr(view) and str(elsewhere) not in repr(view)


async def test_the_deployed_column_names_the_version_the_last_write_used(staged: Staged) -> None:
    assert (await staged.view()).deployed is None
    assert await staged.deploy() is DeployResultStatus.WRITTEN
    view = await staged.view()
    assert view.deployed is not None
    assert view.deployed.short_hash == view.managed.short_hash
    assert view.managed.number == view.deployed.number


async def reveal(
    staged: Staged,
    *,
    columns: set[str] | None = None,
    managed: int | None = None,
    deployed: int | None = None,
    target_hash: str | None = None,
    view: DriftView | None = None,
) -> RevealedDrift:
    """Reveal with the numbers and hash of the view the page was built from."""
    shown = view or await staged.view()
    return await reveal_drift(
        staged.h.db,
        staged.h.crypto,
        staged.header,
        actor=staged.h.seed.users.admin,
        ip_address="203.0.113.7",
        columns=columns or {"target", "deployed", "managed"},
        managed_number=managed if managed is not None else shown.managed.number,
        deployed_number=deployed
        if deployed is not None
        else (shown.deployed.number if shown.deployed else None),
        target_hash=target_hash if target_hash is not None else shown.target_hash,
    )


async def drifted(staged: Staged) -> DriftView:
    """Deploy MANAGED, edit the target on disk and return the drift view."""
    assert await staged.deploy() is DeployResultStatus.WRITTEN
    staged.target.write_text("A=9\nC=3\nSHARED=same\n", encoding="utf-8")
    return await staged.view()


async def test_a_reveal_returns_only_the_values_of_differing_keys_and_audits_each_column(
    staged: Staged,
) -> None:
    view = await drifted(staged)
    revealed = await reveal(staged, view=view)

    assert revealed.values == {
        0: {"target": "9", "deployed": "1", "managed": "1"},
        1: {"target": "3"},
        2: {"deployed": "2", "managed": "2"},
    }
    flat = repr(revealed.values)
    assert "same" not in flat
    entries = await audit_of(staged.h.db, "environment.reveal")
    by_column = {entry.details["column"]: entry for entry in entries}
    assert set(by_column) == {"target", "deployed", "managed"}
    assert by_column["target"].version_ids == []
    assert by_column["deployed"].version_ids and (
        by_column["deployed"].version_ids == by_column["managed"].version_ids
    )
    for entry in entries:
        assert entry.result is AuditResult.SUCCESS and entry.ip_address == "203.0.113.7"
        assert entry.details["scope"] == "drift"
        assert entry.environment_id == staged.environment.id
    assert by_column["target"].details["keys"] == ["A", "C"]
    assert by_column["deployed"].details["keys"] == ["A", "B"]
    assert all(value not in str(entry.details) for entry in entries for value in ("same", "=9"))


async def test_a_reveal_of_one_column_audits_only_that_column(staged: Staged) -> None:
    view = await drifted(staged)
    revealed = await reveal(staged, columns={"target"}, view=view)
    assert revealed.values == {0: {"target": "9"}, 1: {"target": "3"}}
    (entry,) = await audit_of(staged.h.db, "environment.reveal")
    assert entry.details["column"] == "target"
    assert (await reveal(staged, columns={"unknown"}, view=view)).values == {}
    assert len(await audit_of(staged.h.db, "environment.reveal")) == 1


async def test_a_column_that_is_not_available_reveals_nothing_and_writes_no_entry(
    staged: Staged,
) -> None:
    revealed = await reveal(staged, columns={"target", "deployed"})
    assert revealed.values == {}
    assert await audit_of(staged.h.db, "environment.reveal") == []


async def test_a_reveal_of_a_whole_file_audits_the_file_scope_without_key_names(
    staged: Staged,
) -> None:
    web = staged.environment
    await create_version(
        staged.h.db, staged.h.crypto, web.id, CERTIFICATE, author=staged.h.seed.users.admin
    )
    await staged.h.db.commit()
    staged.target.write_text(CERTIFICATE + "extra\n", encoding="utf-8")
    revealed = await reveal(staged)
    assert revealed.values == {0: {"target": CERTIFICATE + "extra\n", "managed": CERTIFICATE}}
    entries = await audit_of(staged.h.db, "environment.reveal")
    assert {entry.details["scope"] for entry in entries} == {"file"}
    assert all("keys" not in entry.details for entry in entries)


async def test_a_reveal_refuses_every_change_since_the_page_and_audits_nothing(
    staged: Staged,
) -> None:
    view = await drifted(staged)
    with pytest.raises(DriftChangedError):
        await reveal(staged, view=view, managed=view.managed.number + 1)
    with pytest.raises(DriftChangedError):
        await reveal(staged, view=view, deployed=99)
    with pytest.raises(DriftChangedError):
        await reveal(staged, view=view, target_hash="0" * 64)
    staged.target.write_text("A=changed-after-render\n", encoding="utf-8")
    with pytest.raises(DriftChangedError):
        await reveal(staged, view=view)
    await create_version(staged.h.db, staged.h.crypto, staged.environment.id, "Z=1\n", author=None)
    await staged.h.db.commit()
    with pytest.raises(DriftChangedError):
        await reveal(staged, view=view)
    assert await audit_of(staged.h.db, "environment.reveal") == []


async def import_it(
    staged: Staged, view: DriftView | None = None, **overrides: object
) -> EnvironmentVersion:
    """Import with the numbers and hash of the view the page was built from."""
    shown = view or await staged.view()
    arguments: dict[str, object] = {
        "base_number": shown.managed.number,
        "target_hash": shown.target_hash or "",
    }
    arguments.update(overrides)
    return await import_target(
        staged.h.db,
        staged.h.crypto,
        staged.header,
        actor=staged.h.seed.users.admin,
        ip_address="203.0.113.7",
        **arguments,  # type: ignore[arg-type]
    )


async def test_importing_the_target_saves_its_bytes_as_the_next_version_with_an_audit_entry(
    staged: Staged,
) -> None:
    view = await drifted(staged)
    before = await staged.versions()
    version = await import_it(staged, view)

    assert version.number == view.managed.number + 1 and version.comment == IMPORT_COMMENT
    assert await staged.versions() == before + 1
    assert version.content_hash == view.target_hash
    assert await version_text(staged.h.db, staged.h.crypto, staged.environment.id) == (
        "A=9\nC=3\nSHARED=same\n"
    )
    (entry,) = await audit_of(staged.h.db, "environment.import")
    assert entry.result is AuditResult.SUCCESS and entry.version_ids == [version.id]
    assert entry.details == {"number": version.number, "source": "target"}
    assert entry.actor_name == staged.h.seed.users.admin.name
    assert "A=9" not in str(entry.details)
    after = await staged.view()
    assert after.kind is DriftKind.NO_DRIFT and after.message.endswith("managed version.")


async def test_an_imported_target_with_crlf_line_endings_keeps_them_so_the_next_deploy_is_clean(
    staged: Staged,
) -> None:
    assert await staged.deploy() is DeployResultStatus.WRITTEN
    staged.target.write_bytes(b"A=1\r\nB=edited\r\n")
    version = await import_it(staged)
    text = await version_text(staged.h.db, staged.h.crypto, staged.environment.id, version.number)
    assert text == "A=1\r\nB=edited\r\n"
    assert await staged.deploy() is DeployResultStatus.UNCHANGED
    assert staged.target.read_bytes() == b"A=1\r\nB=edited\r\n"
    assert files_in(staged.target.parent) == [staged.environment.name]
    assert (await staged.view()).kind is DriftKind.NO_DRIFT


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("stale", STALE_MESSAGE),
        ("changed", "The target file changed since this page was opened. Reload the page."),
        ("same", "The target already has the same content as the managed version."),
    ],
)
async def test_an_import_that_cannot_proceed_is_refused_and_saves_nothing(
    staged: Staged, reason: str, expected: str
) -> None:
    view = await drifted(staged)
    before = await staged.versions()
    if reason == "stale":
        with pytest.raises(DriftError) as refused:
            await import_it(staged, view, base_number=view.managed.number - 1)
    elif reason == "changed":
        staged.target.write_text("A=changed-after-render\n", encoding="utf-8")
        with pytest.raises(DriftError) as refused:
            await import_it(staged, view)
    else:
        staged.target.write_text(MANAGED, encoding="utf-8")
        with pytest.raises(DriftError) as refused:
            await import_it(staged)
    assert str(refused.value).startswith(expected)
    assert await staged.versions() == before
    assert await audit_of(staged.h.db, "environment.import") == []


async def test_an_import_refuses_a_target_that_cannot_be_stored(staged: Staged) -> None:
    before = await staged.versions()
    staged.target.write_text("A=1\x00B=2\n", encoding="utf-8")
    with pytest.raises(DriftError, match="Binary content is not supported"):
        await import_it(staged)
    staged.target.write_text("A=" + "x" * (300 * 1024) + "\n", encoding="utf-8")
    with pytest.raises(DriftError, match="larger than 256 KiB"):
        await import_it(staged)
    staged.target.write_bytes(b"A=\xff\n")
    with pytest.raises(DriftError, match="UTF-8"):
        await import_it(staged)
    staged.target.unlink()
    with pytest.raises(DriftError, match="does not exist"):
        await import_it(staged, target_hash="")
    assert await staged.versions() == before


async def test_an_import_compares_the_hash_without_trusting_a_prefix(staged: Staged) -> None:
    view = await drifted(staged)
    assert view.target_hash is not None
    with pytest.raises(DriftError):
        await import_it(staged, view, target_hash=view.target_hash[:7])
    with pytest.raises(DriftError):
        await import_it(staged, view, target_hash="")


async def overwrite(staged: Staged, base: int | None = None) -> OverwriteResult:
    """Overwrite as the seeded admin."""
    view = await staged.view()
    return await overwrite_target(
        staged.h.db,
        staged.h.engine,
        await staged.h.scope_of("admin"),
        AuditActor.for_user(staged.h.seed.users.admin, "203.0.113.7"),
        staged.header,
        base_number=base if base is not None else view.managed.number,
    )


async def test_overwriting_replaces_the_target_after_keeping_a_backup_with_mode_0600(
    staged: Staged,
) -> None:
    view = await drifted(staged)
    result = await overwrite(staged)

    assert result.version_number == view.managed.number
    assert result.backed_up and not result.unchanged
    assert staged.target.read_text(encoding="utf-8") == MANAGED
    (backup,) = [entry for entry in staged.target.parent.iterdir() if ".bak-" in entry.name]
    assert backup.read_text(encoding="utf-8") == "A=9\nC=3\nSHARED=same\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    _first, entry = await audit_of(staged.h.db, "service.deploy")
    assert entry.details["force"] is True and entry.actor_name == staged.h.seed.users.admin.name
    assert (await staged.view()).kind is DriftKind.NO_DRIFT


async def use_the_overwrite_policy(staged: Staged) -> None:
    """Let the staging deployment replace a drifted target without asking."""
    await set_deployment(
        staged.h.db,
        staged.h.seed.deployments.rentals_staging.id,
        drift_policy=DriftPolicy.OVERWRITE,
    )


async def test_overwriting_keeps_a_backup_even_under_the_overwrite_policy(
    staged: Staged,
) -> None:
    await use_the_overwrite_policy(staged)
    await drifted(staged)
    result = await overwrite(staged)
    assert result.backed_up
    assert len([n for n in files_in(staged.target.parent) if ".bak-" in n]) == 1


async def test_a_deploy_without_the_drift_view_still_skips_the_backup_under_overwrite(
    staged: Staged,
) -> None:
    await use_the_overwrite_policy(staged)
    await drifted(staged)
    assert await staged.deploy() is DeployResultStatus.WRITTEN
    assert files_in(staged.target.parent) == [staged.environment.name]


async def test_an_overwrite_is_refused_when_nothing_drifted_or_the_page_is_old(
    staged: Staged,
) -> None:
    view = await staged.view()
    staged.target.write_text(MANAGED, encoding="utf-8")
    with pytest.raises(DriftError, match="nothing to overwrite"):
        await overwrite(staged)
    staged.target.write_text("A=edited\n", encoding="utf-8")
    with pytest.raises(DriftError) as old:
        await overwrite(staged, base=view.managed.number - 1)
    assert str(old.value) == STALE_MESSAGE
    assert staged.target.read_text(encoding="utf-8") == "A=edited\n"
    assert files_in(staged.target.parent) == [staged.environment.name]
    assert await audit_of(staged.h.db, "service.deploy") == []


@pytest.mark.parametrize("state", ["missing", "unreadable", "outside"])
async def test_an_overwrite_is_refused_where_the_engine_could_only_fail(
    staged: Staged, tmp_path: Path, state: str
) -> None:
    if state == "unreadable":
        staged.target.write_bytes(b"\xff\xfe")
    elif state == "outside":
        await staged.h.db.execute(delete(AllowedPath))
        await staged.h.db.commit()
    with pytest.raises(DriftError):
        await overwrite(staged)
    assert await audit_of(staged.h.db, "service.deploy") == []


async def test_an_overwrite_while_another_deploy_holds_the_lock_changes_nothing(
    staged: Staged,
) -> None:
    await drifted(staged)
    with (
        hold_deploy_lock(staged.h.lock_dir, str(staged.h.roots["staging"])),
        pytest.raises(DriftError, match="Another deploy is running"),
    ):
        await overwrite(staged)
    assert staged.target.read_text(encoding="utf-8") == "A=9\nC=3\nSHARED=same\n"
    assert files_in(staged.target.parent) == [staged.environment.name]


async def test_the_audit_entries_of_every_action_hold_no_value_or_path(staged: Staged) -> None:
    view = await drifted(staged)
    await reveal(staged, view=view)
    await import_it(staged, view)
    rows = (await staged.h.db.execute(select(AuditEntry))).scalars().all()
    flat = " ".join(f"{entry.details} {entry.target_name}" for entry in rows)
    root = str(staged.h.roots["staging"])
    assert root not in flat
    for value in ("A=9", "SHARED", "same"):
        assert value not in " ".join(
            str(entry.details) for entry in rows if entry.action.startswith("environment.")
        )
