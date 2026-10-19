"""Dist checks: the outcome of comparing an environment with its dist file under the deploy policy.

A check reads the latest saved version and the dist file from the target, compares key names and
applies the missing-keys policy (the environment's own, else the deployment's). It never holds or
returns a value: the audit entry, the result and every message carry key counts, version numbers
and fixed reasons only. Allowed paths are loaded fresh for every check, so a path removed from
the list stops the next check at once. The same checks serve the report screen, the deployment
overview and later the API and the deploy sequence.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics, telemetry
from app.core.crypto import CryptoService
from app.models import (
    AuditResult,
    DistMissingPolicy,
    DistOutcome,
    EnvironmentVersion,
    User,
)
from app.models.types import utc_now
from app.services.audit import AuditActor, EnvironmentRef, record_environment_audit
from app.services.dist import (
    DistError,
    DistProblem,
    DistReport,
    DistSkipReason,
    compare_to_dist,
    read_dist_file,
)
from app.services.editor import (
    EditorError,
    EditorHeader,
    KeyValueSubmission,
    save_version,
    stored_version,
)
from app.services.env_parser import parse_env
from app.services.notify_events import NO_EVENT_NOTIFIER, EventNotifier
from app.services.paths import load_allowed_paths
from app.services.permissions import AccessScope, Capability, ItemLevel, load_items
from app.services.structure_lookup import environment_chain

ADDED_COMMENT = "Added missing keys from the dist file"
STALE_MESSAGE = "This file changed since the check. The report below is up to date."
NOTHING_TO_ADD_MESSAGE = "No keys are missing."

PROBLEM_MESSAGES = {
    DistProblem.ENVIRONMENT_MISSING: "This file does not exist.",
    DistProblem.NO_VERSION: "No version is saved for this file yet. There is nothing to compare.",
    DistProblem.ROOT_OUTSIDE: (
        "The deployment is outside the allowed paths. Dist files are not read from it."
    ),
    DistProblem.NOT_RELATIVE: "The dist path must be a relative path inside the deployment.",
    DistProblem.LEAVES_ROOT: "The dist path leaves the deployment root.",
    DistProblem.NOT_A_FILE: "The dist file does not exist or is not a regular file.",
    DistProblem.UNREADABLE: "The dist file cannot be read.",
    DistProblem.TOO_LARGE: "The dist file is too large.",
    DistProblem.NOT_UTF8: "The dist file is not valid UTF-8 text.",
}

SKIP_MESSAGES = {
    DistSkipReason.ENVIRONMENT_NOT_ENV_FORMAT: (
        "This file is not env format (certificate). Key comparison is skipped for it."
    ),
    DistSkipReason.DIST_NOT_ENV_FORMAT: (
        "The dist file is not env format. Key comparison is skipped for it."
    ),
    DistSkipReason.NO_DIST_LINKED: (
        "No dist file is linked to this file. Key comparison is skipped for it."
    ),
}


@dataclass(frozen=True)
class DistSubject:
    """The environment a check ran for, with the ids of its ancestors for the audit entry."""

    environment_id: int
    name: str
    service_id: int
    service_name: str
    deployment_id: int
    project_id: int
    dist_path: str | None


@dataclass(frozen=True)
class DistCheck:
    """One finished check.

    ``report`` is set unless the check failed; a skipped comparison has a report with its skip
    reason. ``problem`` is set only for a failed check. ``version`` is the number of the saved
    version that was compared.
    """

    subject: DistSubject
    outcome: DistOutcome
    policy: DistMissingPolicy
    version: int | None
    version_id: int | None
    report: DistReport | None
    problem: DistProblem | None
    checked_at: datetime
    checked_by: str

    @property
    def problem_message(self) -> str | None:
        """Fixed text for a failed check, never naming a host path."""
        return PROBLEM_MESSAGES[self.problem] if self.problem else None

    @property
    def skip_message(self) -> str | None:
        """Fixed text for a skipped comparison."""
        if self.report is None or self.report.skipped is None:
            return None
        return SKIP_MESSAGES[self.report.skipped]


async def _load_subject(
    db: AsyncSession, environment_id: int
) -> tuple[DistSubject, str, DistMissingPolicy] | None:
    """The environment, its deployment root and the policy in force, or None when it is gone."""
    chain = await environment_chain(db, environment_id)
    if chain is None:
        return None
    environment, service, deployment = chain
    subject = DistSubject(
        environment_id=environment.id,
        name=environment.name,
        service_id=service.id,
        service_name=service.name,
        deployment_id=deployment.id,
        project_id=deployment.project_id,
        dist_path=environment.dist_path,
    )
    policy = environment.dist_missing_policy or deployment.dist_missing_policy
    return subject, deployment.root_path, policy


def _outcome(report: DistReport, policy: DistMissingPolicy) -> DistOutcome:
    """The decision for a finished comparison."""
    if report.skipped is not None:
        return DistOutcome.SKIPPED
    if report.missing:
        return DistOutcome.BLOCKED if policy is DistMissingPolicy.BLOCK else DistOutcome.WARNING
    return DistOutcome.PASSED


async def evaluate_dist(
    db: AsyncSession,
    crypto: CryptoService,
    environment_id: int,
    *,
    checked_by: str,
    version_number: int | None = None,
) -> DistCheck:
    """Compare a saved version of an environment with its dist file, without auditing.

    The version is the one with ``version_number``, else the newest; a deploy names the version it
    is about to write so a save made meanwhile cannot change what was checked. A file that is not
    env format is skipped before anything is read from the host, and so is a file without a dist
    link. Otherwise the dist file is read with allowed paths loaded now. Every refusal to read
    becomes a failed check; nothing here raises for them.

    The check runs in a span that carries ids and the outcome, and a check that could not run is
    counted by its problem.
    """
    with telemetry.span(
        telemetry.DIST_CHECK_SPAN, {telemetry.ENVIRONMENT_ID: environment_id}
    ) as active:
        try:
            check = await _evaluate_dist(db, crypto, environment_id, checked_by, version_number)
        except DistError as error:
            metrics.record_dist_check_failure(error.problem.value)
            raise
        subject = check.subject
        telemetry.annotate(
            active,
            {
                telemetry.PROJECT_ID: subject.project_id,
                telemetry.DEPLOYMENT_ID: subject.deployment_id,
                telemetry.SERVICE_ID: subject.service_id,
                telemetry.ENVIRONMENT_NAME: subject.name,
                telemetry.DIST_OUTCOME: check.outcome.value,
                telemetry.DIST_PROBLEM: check.problem.value if check.problem else None,
            },
        )
        if check.outcome is DistOutcome.FAILED and check.problem is not None:
            metrics.record_dist_check_failure(check.problem.value)
        return check


async def _evaluate_dist(
    db: AsyncSession,
    crypto: CryptoService,
    environment_id: int,
    checked_by: str,
    version_number: int | None,
) -> DistCheck:
    """The comparison itself, without the span and the metric."""
    loaded = await _load_subject(db, environment_id)
    if loaded is None:
        missing = DistProblem.ENVIRONMENT_MISSING
        raise DistError(missing, PROBLEM_MESSAGES[missing])
    subject, root_path, policy = loaded
    latest = await stored_version(db, environment_id, version_number)

    def finish(
        outcome: DistOutcome,
        report: DistReport | None = None,
        problem: DistProblem | None = None,
    ) -> DistCheck:
        return DistCheck(
            subject=subject,
            outcome=outcome,
            policy=policy,
            version=latest.number if latest else None,
            version_id=latest.id if latest else None,
            report=report,
            problem=problem,
            checked_at=utc_now(),
            checked_by=checked_by,
        )

    if latest is None:
        return finish(DistOutcome.FAILED, problem=DistProblem.NO_VERSION)
    environment = parse_env(crypto.decrypt_text(latest.content_encrypted))
    if not environment.is_env_format:
        report = DistReport(DistSkipReason.ENVIRONMENT_NOT_ENV_FORMAT, (), (), ())
        return finish(DistOutcome.SKIPPED, report)
    if not subject.dist_path:
        return finish(DistOutcome.SKIPPED, DistReport(DistSkipReason.NO_DIST_LINKED, (), (), ()))
    try:
        dist_text = read_dist_file(await load_allowed_paths(db), root_path, subject.dist_path)
    except DistError as error:
        return finish(DistOutcome.FAILED, problem=error.problem)
    report = compare_to_dist(environment, parse_env(dist_text))
    return finish(_outcome(report, policy), report)


def _audit_result(outcome: DistOutcome) -> AuditResult:
    """A failed check is a failure, a blocked one is blocked, everything else a success."""
    if outcome is DistOutcome.FAILED:
        return AuditResult.FAILURE
    if outcome is DistOutcome.BLOCKED:
        return AuditResult.BLOCKED
    return AuditResult.SUCCESS


async def record_dist_check(db: AsyncSession, check: DistCheck, *, actor: AuditActor) -> None:
    """Add the audit entry of a check to the session; the caller commits.

    The actor is a user, an API token or the CLI, so the web screens, the API and the deploy
    sequence audit their checks the same way. Details hold the outcome, the policy, key counts
    and the skip reason or problem code, never a key value.
    """
    report = check.report
    details: dict[str, object] = {
        "outcome": check.outcome.value,
        "policy": check.policy.value,
        "version": check.version,
    }
    if report is not None and report.skipped is None:
        details["missing"] = len(report.missing)
        details["extra"] = len(report.extra)
        details["empty"] = len(report.empty)
    if report is not None and report.skipped is not None:
        details["skipped"] = report.skipped.value
    if check.problem is not None:
        details["problem"] = check.problem.value
    subject = check.subject
    await record_environment_audit(
        db,
        actor,
        EnvironmentRef(
            subject.environment_id,
            subject.name,
            subject.project_id,
            subject.deployment_id,
            subject.service_id,
        ),
        action="environment.dist_check",
        result=_audit_result(check.outcome),
        details=details,
        version_ids=[check.version_id] if check.version_id is not None else (),
        created_at=check.checked_at,
    )


async def run_dist_check(
    db: AsyncSession,
    crypto: CryptoService,
    environment_id: int,
    *,
    actor: User,
    ip_address: str | None,
) -> DistCheck:
    """Run a check for one environment and audit it."""
    check = await evaluate_dist(db, crypto, environment_id, checked_by=actor.name)
    await record_dist_check(db, check, actor=AuditActor.for_user(actor, ip_address))
    await db.commit()
    return check


async def run_deployment_dist_checks(
    db: AsyncSession,
    crypto: CryptoService,
    scope: AccessScope,
    deployment_id: int,
    *,
    actor: User,
    ip_address: str | None,
) -> tuple[DistCheck, ...]:
    """Check every environment of a deployment the caller may run dist checks for.

    Environments outside the caller's scope are neither checked nor named. Each check is
    audited on its own; the rows come back ordered by service and file name.
    """
    environment_ids = [
        item.item_id
        for item in await load_items(db, ItemLevel.ENVIRONMENT)
        if item.deployment_id == deployment_id and scope.can(Capability.DEPLOY, item)
    ]
    checks = [
        await evaluate_dist(db, crypto, environment_id, checked_by=actor.name)
        for environment_id in environment_ids
    ]
    audit_actor = AuditActor.for_user(actor, ip_address)
    for check in checks:
        await record_dist_check(db, check, actor=audit_actor)
    await db.commit()
    return tuple(sorted(checks, key=lambda check: (check.subject.service_name, check.subject.name)))


@dataclass(frozen=True)
class AddedKeys:
    """The version that holds the added keys and their names."""

    version: EnvironmentVersion
    keys: tuple[str, ...]


async def add_missing_keys(
    db: AsyncSession,
    crypto: CryptoService,
    header: EditorHeader,
    *,
    actor: User,
    ip_address: str | None,
    base_number: int,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> AddedKeys:
    """Save the keys the dist file defines and the environment lacks, with empty values.

    The keys are worked out here from a fresh comparison, never taken from the request, and the
    new version goes through the editor's own save, so it is audited like any other save.
    ``base_number`` is the version the report was built from; a newer version refuses the call.
    Raises ``EditorError`` when the save is refused or there is nothing to add.
    """
    check = await evaluate_dist(db, crypto, header.id, checked_by=actor.name)
    if check.version != base_number:
        raise EditorError(STALE_MESSAGE)
    if check.outcome is DistOutcome.FAILED:
        raise EditorError(check.problem_message or "The check could not run.")
    if check.report is None or check.report.skipped is not None:
        raise EditorError(check.skip_message or "Key comparison is skipped for this file.")
    if not check.report.missing:
        raise EditorError(NOTHING_TO_ADD_MESSAGE)
    keys = check.report.missing
    version = await save_version(
        db,
        crypto,
        header,
        actor=actor,
        ip_address=ip_address,
        base_number=base_number,
        comment=ADDED_COMMENT,
        key_values=KeyValueSubmission(added=[(key, "") for key in keys]),
        notifier=notifier,
    )
    return AddedKeys(version=version, keys=keys)
