"""Deploy decisions: drift state, the policies applied to one environment and the run status.

These are pure functions of what the checks found. A missing-keys block (or a dist check that
could not run under the block policy) comes first, then a missing target, then drift. Force
overrides every block and halt and is reported in the notes of the plan. A drifted target is
backed up before it is replaced unless the policy is to overwrite it, so a forced deploy never
destroys on-disk changes without a copy.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.models import (
    DeployResultStatus,
    DeployStatus,
    DistMissingPolicy,
    DistOutcome,
    DriftPolicy,
    DriftState,
    MissingTargetPolicy,
)
from app.services.deploy_types import EnvironmentOutcome
from app.services.dist_check import DistCheck

HALT_DRIFT_MESSAGE = "The target changed outside the app and the drift policy halts."
HALT_MISSING_MESSAGE = "The target file does not exist and the policy halts."
FORCED_DIST_NOTE = "Forced past the dist check."
FORCED_DRIFT_NOTE = "Forced past drift."
FORCED_MISSING_NOTE = "Forced past the missing target policy."


class Action(StrEnum):
    """What the policies decide for one environment."""

    BLOCK = "block"
    HALT = "halt"
    UNCHANGED = "unchanged"
    WRITE = "write"


@dataclass(frozen=True)
class Plan:
    """The decision for one environment, with the drift state it was based on.

    ``message`` is set for a block or halt, ``notes`` carry warnings and forced overrides to
    append to the message of a write.
    """

    action: Action
    drift: DriftState
    message: str = ""
    backup: bool = False
    created: bool = False
    forced: bool = False
    notes: tuple[str, ...] = ()


def overall_status(outcomes: tuple[EnvironmentOutcome, ...]) -> DeployStatus:
    """The run status: Failed beats Blocked beats Halted beats Success."""
    statuses = {outcome.status for outcome in outcomes}
    if DeployResultStatus.FAILED in statuses:
        return DeployStatus.FAILED
    if DeployResultStatus.BLOCKED in statuses:
        return DeployStatus.BLOCKED
    if DeployResultStatus.HALTED in statuses:
        return DeployStatus.HALTED
    return DeployStatus.SUCCESS


def drift_state(
    current: bytes | None, current_hash: str | None, version_hash: str, baseline: str | None
) -> DriftState:
    """How the target on disk stands against what this app last wrote.

    A file equal to the version is in sync. Otherwise it is in sync when it equals the last
    content this app wrote (``baseline``); a target this app never wrote is drift unless it is
    empty.
    """
    if current is None:
        return DriftState.MISSING
    if current_hash == version_hash:
        return DriftState.IN_SYNC
    if baseline is not None:
        return DriftState.IN_SYNC if current_hash == baseline else DriftState.DRIFT
    return DriftState.DRIFT if current.strip() else DriftState.IN_SYNC


def blocked_message(check: DistCheck) -> str:
    """Fixed text for a dist block: the number of missing keys or why the check could not run."""
    if check.outcome is DistOutcome.FAILED:
        return f"Blocked: the dist check could not run. {check.problem_message}"
    count = len(check.report.missing) if check.report else 0
    noun = "key is" if count == 1 else "keys are"
    return f"Blocked: {count} dist {noun} missing from this file."


def _dist_gate(check: DistCheck, force: bool) -> tuple[str | None, list[str], bool]:
    """The message of a dist block, if one stops the deploy, with the notes and the forced flag."""
    notes: list[str] = []
    if check.outcome is DistOutcome.WARNING:
        notes.append("Dist keys are missing from this file (the policy warns).")
    elif check.outcome is DistOutcome.FAILED and check.policy is DistMissingPolicy.WARN:
        notes.append("The dist check could not run (the policy warns).")
    blocks = check.outcome is DistOutcome.BLOCKED or (
        check.outcome is DistOutcome.FAILED and check.policy is DistMissingPolicy.BLOCK
    )
    if not blocks:
        return None, notes, False
    if not force:
        return blocked_message(check), notes, False
    notes.append(FORCED_DIST_NOTE)
    return None, notes, True


def plan_environment(
    *,
    drift: DriftState,
    unchanged: bool,
    check: DistCheck,
    drift_policy: DriftPolicy,
    missing_policy: MissingTargetPolicy,
    force: bool,
) -> Plan:
    """Apply the dist, missing target and drift policies to what the checks found."""
    block_message, notes, forced = _dist_gate(check, force)
    if block_message is not None:
        return Plan(Action.BLOCK, drift, block_message)
    if drift is DriftState.MISSING:
        if missing_policy is MissingTargetPolicy.HALT:
            if not force:
                return Plan(Action.HALT, drift, HALT_MISSING_MESSAGE)
            forced = True
            notes.append(FORCED_MISSING_NOTE)
        return Plan(Action.WRITE, drift, created=True, forced=forced, notes=tuple(notes))
    if unchanged:
        return Plan(Action.UNCHANGED, drift, forced=forced, notes=tuple(notes))
    if drift is DriftState.DRIFT and drift_policy is DriftPolicy.HALT:
        if not force:
            return Plan(Action.HALT, drift, HALT_DRIFT_MESSAGE)
        forced = True
        notes.append(FORCED_DRIFT_NOTE)
    # A halting policy only reaches this point with force set; the drifted file is kept either way.
    backup = drift is DriftState.DRIFT and drift_policy is not DriftPolicy.OVERWRITE
    return Plan(Action.WRITE, drift, backup=backup, forced=forced, notes=tuple(notes))
