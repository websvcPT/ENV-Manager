"""The deploy decisions: drift state, policy order, force and the run status."""

import pytest

from app.models import (
    DeployResultStatus,
    DeployStatus,
    DistMissingPolicy,
    DistOutcome,
    DriftPolicy,
    DriftState,
    MissingTargetPolicy,
)
from app.models.types import utc_now
from app.services.deploy_policy import (
    Action,
    drift_state,
    overall_status,
    plan_environment,
)
from app.services.deploy_types import EnvironmentOutcome
from app.services.dist import DistProblem, DistReport
from app.services.dist_check import DistCheck, DistSubject

VERSION = "version-hash"
BASELINE = "baseline-hash"


def make_check(
    outcome: DistOutcome = DistOutcome.PASSED,
    policy: DistMissingPolicy = DistMissingPolicy.BLOCK,
    missing: tuple[str, ...] = (),
) -> DistCheck:
    """A finished dist check with the given outcome."""
    failed = outcome is DistOutcome.FAILED
    return DistCheck(
        subject=DistSubject(1, ".env", 1, "web", 1, 1, "environments/.env-dist"),
        outcome=outcome,
        policy=policy,
        version=1,
        version_id=1,
        report=None if failed else DistReport(None, missing, (), ()),
        problem=DistProblem.NOT_A_FILE if failed else None,
        checked_at=utc_now(),
        checked_by="root",
    )


def plan(
    *,
    current: bytes | None = b"disk",
    baseline: str | None = BASELINE,
    current_hash: str | None = "disk-hash",
    check: DistCheck | None = None,
    drift_policy: DriftPolicy = DriftPolicy.HALT,
    missing_policy: MissingTargetPolicy = MissingTargetPolicy.CREATE,
    force: bool = False,
) -> tuple[Action, DriftState]:
    """The action and drift state for a target on disk."""
    state = drift_state(current, current_hash, VERSION, baseline)
    result = plan_environment(
        drift=state,
        unchanged=current_hash == VERSION,
        check=check or make_check(),
        drift_policy=drift_policy,
        missing_policy=missing_policy,
        force=force,
    )
    return result.action, result.drift


def test_a_missing_target_is_missing_and_an_identical_one_is_in_sync() -> None:
    assert drift_state(None, None, VERSION, None) is DriftState.MISSING
    assert drift_state(b"x", VERSION, VERSION, None) is DriftState.IN_SYNC


def test_a_file_equal_to_the_last_written_content_is_in_sync_otherwise_drift() -> None:
    assert drift_state(b"x", BASELINE, VERSION, BASELINE) is DriftState.IN_SYNC
    assert drift_state(b"x", "edited", VERSION, BASELINE) is DriftState.DRIFT


def test_the_first_deploy_to_an_existing_file_is_drift_unless_it_is_empty() -> None:
    assert drift_state(b"x", "edited", VERSION, None) is DriftState.DRIFT
    assert drift_state(b"", "empty-hash", VERSION, None) is DriftState.IN_SYNC
    assert drift_state(b" \n\t", "blank-hash", VERSION, None) is DriftState.IN_SYNC


def test_an_empty_file_is_not_forgiven_once_the_app_has_written_the_target() -> None:
    assert drift_state(b"", "empty-hash", VERSION, BASELINE) is DriftState.DRIFT


@pytest.mark.parametrize(
    ("policy", "force", "action"),
    [
        (DriftPolicy.HALT, False, Action.HALT),
        (DriftPolicy.HALT, True, Action.WRITE),
        (DriftPolicy.OVERWRITE, False, Action.WRITE),
        (DriftPolicy.BACKUP_OVERWRITE, False, Action.WRITE),
    ],
)
def test_drift_follows_the_policy_and_force_overrides_a_halt(
    policy: DriftPolicy, force: bool, action: Action
) -> None:
    assert plan(drift_policy=policy, force=force) == (action, DriftState.DRIFT)


def test_only_the_backup_policy_asks_for_a_backup() -> None:
    for policy, backup in (
        (DriftPolicy.HALT, False),
        (DriftPolicy.OVERWRITE, False),
        (DriftPolicy.BACKUP_OVERWRITE, True),
    ):
        result = plan_environment(
            drift=DriftState.DRIFT,
            unchanged=False,
            check=make_check(),
            drift_policy=policy,
            missing_policy=MissingTargetPolicy.CREATE,
            force=True,
        )
        assert result.backup is backup


@pytest.mark.parametrize(
    ("policy", "force", "action"),
    [
        (MissingTargetPolicy.CREATE, False, Action.WRITE),
        (MissingTargetPolicy.HALT, False, Action.HALT),
        (MissingTargetPolicy.HALT, True, Action.WRITE),
    ],
)
def test_a_missing_target_is_created_or_halts(
    policy: MissingTargetPolicy, force: bool, action: Action
) -> None:
    assert plan(current=None, current_hash=None, missing_policy=policy, force=force) == (
        action,
        DriftState.MISSING,
    )


def test_a_file_that_matches_the_version_is_unchanged_even_after_an_outside_edit() -> None:
    assert plan(current_hash=VERSION) == (Action.UNCHANGED, DriftState.IN_SYNC)
    assert plan(current_hash=VERSION, baseline=None) == (Action.UNCHANGED, DriftState.IN_SYNC)


@pytest.mark.parametrize(
    ("outcome", "policy", "force", "action"),
    [
        (DistOutcome.BLOCKED, DistMissingPolicy.BLOCK, False, Action.BLOCK),
        (DistOutcome.BLOCKED, DistMissingPolicy.BLOCK, True, Action.WRITE),
        (DistOutcome.WARNING, DistMissingPolicy.WARN, False, Action.WRITE),
        (DistOutcome.FAILED, DistMissingPolicy.BLOCK, False, Action.BLOCK),
        (DistOutcome.FAILED, DistMissingPolicy.WARN, False, Action.WRITE),
        (DistOutcome.SKIPPED, DistMissingPolicy.BLOCK, False, Action.WRITE),
        (DistOutcome.PASSED, DistMissingPolicy.BLOCK, False, Action.WRITE),
    ],
)
def test_the_dist_outcome_blocks_warns_or_passes(
    outcome: DistOutcome, policy: DistMissingPolicy, force: bool, action: Action
) -> None:
    check = make_check(outcome, policy, missing=("A",) if outcome is DistOutcome.BLOCKED else ())
    result = plan(
        current_hash=None, current=None, check=check, force=force, drift_policy=DriftPolicy.HALT
    )
    assert result[0] is action


def test_a_dist_block_comes_before_drift_and_a_matching_file() -> None:
    blocked = make_check(DistOutcome.BLOCKED, missing=("A", "B"))
    assert plan(check=blocked)[0] is Action.BLOCK
    assert plan(current_hash=VERSION, check=blocked)[0] is Action.BLOCK
    result = plan_environment(
        drift=DriftState.DRIFT,
        unchanged=False,
        check=blocked,
        drift_policy=DriftPolicy.HALT,
        missing_policy=MissingTargetPolicy.CREATE,
        force=False,
    )
    assert result.message == "Blocked: 2 dist keys are missing from this file."


def test_force_is_noted_for_every_override() -> None:
    blocked = make_check(DistOutcome.BLOCKED, missing=("A",))
    result = plan_environment(
        drift=DriftState.DRIFT,
        unchanged=False,
        check=blocked,
        drift_policy=DriftPolicy.HALT,
        missing_policy=MissingTargetPolicy.CREATE,
        force=True,
    )
    assert result.action is Action.WRITE and result.forced
    assert result.notes == ("Forced past the dist check.", "Forced past drift.")


def test_a_warning_adds_a_note_without_forcing() -> None:
    result = plan_environment(
        drift=DriftState.IN_SYNC,
        unchanged=False,
        check=make_check(DistOutcome.WARNING, DistMissingPolicy.WARN, missing=("A",)),
        drift_policy=DriftPolicy.HALT,
        missing_policy=MissingTargetPolicy.CREATE,
        force=False,
    )
    assert result.action is Action.WRITE and not result.forced
    assert "the policy warns" in result.notes[0]


def outcome(status: DeployResultStatus) -> EnvironmentOutcome:
    """An outcome with a status."""
    return EnvironmentOutcome(1, ".env", "environments/.env", 1, 1, status, "")


def test_the_run_status_is_failed_then_blocked_then_halted_then_success() -> None:
    written, halted = outcome(DeployResultStatus.WRITTEN), outcome(DeployResultStatus.HALTED)
    blocked, failed = outcome(DeployResultStatus.BLOCKED), outcome(DeployResultStatus.FAILED)
    assert overall_status((written, halted, blocked, failed)) is DeployStatus.FAILED
    assert overall_status((written, halted, blocked)) is DeployStatus.BLOCKED
    assert overall_status((written, halted)) is DeployStatus.HALTED
    assert overall_status((written, outcome(DeployResultStatus.UNCHANGED))) is DeployStatus.SUCCESS
    assert overall_status((outcome(DeployResultStatus.WOULD_WRITE),)) is DeployStatus.SUCCESS
