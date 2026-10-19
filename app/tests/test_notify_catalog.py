"""Tests for the notification kinds: their switches, defaults and the event each one belongs to."""

from dataclasses import fields
from datetime import UTC, datetime

import pytest

from app.models import BackupStatus, BackupTrigger, DeployStatus, PostDeployStatus
from app.services.app_settings import NotificationSettings
from app.services.backup_events import BackupFinished
from app.services.deploy_events import DeployFinished
from app.services.notify_catalog import (
    EVENT_ROWS,
    PROJECT_KINDS,
    EventKind,
    Recipients,
    is_enabled,
    kind_of,
    recipients_of,
)
from app.services.notify_events import (
    FileChange,
    FileChanged,
    GitPushFailed,
    LockoutScope,
    LockoutStarted,
    MasterKeyRotated,
    SignInFromNewAddress,
    UserChange,
    UserChanged,
)
from app.services.settings_events import SettingsChanged

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def deploy(status: DeployStatus) -> DeployFinished:
    """A finished deploy with the given overall status and no files."""
    return DeployFinished(
        1, 1, 1, "prod", 1, "web", "root", False, status, PostDeployStatus.NONE, ()
    )


def backup(status: BackupStatus) -> BackupFinished:
    """A finished backup with the given status."""
    return BackupFinished(status, BackupTrigger.MANUAL, "b.db", None, None, NOW, "root")


def test_every_switch_and_recipients_choice_is_a_field_of_the_settings() -> None:
    """The catalog and the stored section describe the same fields, so neither can drift.

    The webhook switch and address are the only fields that belong to no row of the matrix.
    """
    names = {item.name for item in fields(NotificationSettings)}
    expected = {row.kind.value for row in EVENT_ROWS} | {
        f"{kind.value}_to" for kind in PROJECT_KINDS
    }
    assert names == expected | {"webhook_enabled", "webhook_url"}


def test_the_defaults_are_the_decided_ones() -> None:
    """Failures, blocks, lockouts and user changes are on; successes and file changes are off."""
    settings = NotificationSettings()
    on = {row.kind for row in EVENT_ROWS if is_enabled(settings, row.kind)}
    assert on == {
        EventKind.LOGIN_NEW_ADDRESS,
        EventKind.LOCKOUT,
        EventKind.DEPLOY_PROBLEM,
        EventKind.USER_CHANGED,
        EventKind.BACKUP_FAILED,
        EventKind.GIT_PUSH_FAILED,
        EventKind.KEY_ROTATED,
    }
    assert not is_enabled(settings, EventKind.DEPLOY_SUCCEEDED)
    assert not is_enabled(settings, EventKind.FILE_CHANGED)


def test_the_allowed_path_announcement_has_no_switch_and_is_always_on() -> None:
    """Changes to the allowed paths are a system notification that cannot be turned off."""
    assert EventKind.SETTINGS_CHANGED not in {row.kind for row in EVENT_ROWS}
    assert is_enabled(NotificationSettings(), EventKind.SETTINGS_CHANGED)


def test_project_events_default_to_the_project_admins_and_system_events_to_superadmins() -> None:
    """Only the kinds about a project have a choice; everything else is for the superadmins."""
    settings = NotificationSettings()
    for kind in EventKind:
        wanted = Recipients.PROJECT_ADMINS if kind in PROJECT_KINDS else Recipients.SUPERADMINS
        assert recipients_of(settings, kind) is wanted
    assert {
        EventKind.DEPLOY_SUCCEEDED,
        EventKind.DEPLOY_PROBLEM,
        EventKind.FILE_CHANGED,
    } == PROJECT_KINDS


def test_a_stored_choice_is_used_and_a_value_that_is_not_a_choice_reads_as_the_default() -> None:
    """The recipients field holds free text, so a bad value never breaks sending."""
    both = NotificationSettings(deploy_problem_to="both")
    assert recipients_of(both, EventKind.DEPLOY_PROBLEM) is Recipients.BOTH
    junk = NotificationSettings(deploy_problem_to="everyone")
    assert recipients_of(junk, EventKind.DEPLOY_PROBLEM) is Recipients.PROJECT_ADMINS


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (DeployStatus.SUCCESS, EventKind.DEPLOY_SUCCEEDED),
        (DeployStatus.FAILED, EventKind.DEPLOY_PROBLEM),
        (DeployStatus.BLOCKED, EventKind.DEPLOY_PROBLEM),
        (DeployStatus.HALTED, EventKind.DEPLOY_PROBLEM),
        (DeployStatus.POST_DEPLOY_FAILED, EventKind.DEPLOY_PROBLEM),
    ],
)
def test_a_deploy_is_a_success_or_a_problem(status: DeployStatus, kind: EventKind) -> None:
    """Every outcome but success is one problem kind, a failed command included."""
    assert kind_of(deploy(status)) is kind


def test_a_backup_notifies_only_when_it_failed_or_could_not_be_copied() -> None:
    """A successful backup says nothing; a failed one and a partial one are the failure kind."""
    assert kind_of(backup(BackupStatus.SUCCESS)) is None
    assert kind_of(backup(BackupStatus.FAILURE)) is EventKind.BACKUP_FAILED
    assert kind_of(backup(BackupStatus.PARTIAL)) is EventKind.BACKUP_FAILED


def test_every_other_event_has_its_kind() -> None:
    """Each event type maps to exactly one kind."""
    assert kind_of(SettingsChanged("allowed_paths", "added", "/opt/x", "root")) is (
        EventKind.SETTINGS_CHANGED
    )
    assert kind_of(SignInFromNewAddress("alice", "203.0.113.5")) is EventKind.LOGIN_NEW_ADDRESS
    assert kind_of(LockoutStarted(LockoutScope.USER, "alice", None, 15)) is EventKind.LOCKOUT
    assert kind_of(UserChanged(UserChange.CREATED, "alice", "root")) is EventKind.USER_CHANGED
    assert kind_of(FileChanged(1, 1, 1, 1, ".env", 2, FileChange.SAVED, "root")) is (
        EventKind.FILE_CHANGED
    )
    assert kind_of(GitPushFailed("authentication failed", 3)) is EventKind.GIT_PUSH_FAILED
    assert kind_of(MasterKeyRotated(10, 2, "cli")) is EventKind.KEY_ROTATED
