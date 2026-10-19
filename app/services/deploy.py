"""The deploy engine: one deploy runs for one service and writes its environments to the host.

For the selected environments of the service that the caller's scope permits, a real deploy takes
the lock of the deployment's real root path, then per environment resolves and validates the
target against freshly loaded allowed paths, runs the audited dist check on the version to write,
compares the file on disk with the last content this app wrote (drift), applies the policies
(``app.services.deploy_policy``), optionally backs the target up and replaces it atomically
(``app.services.target_files``). A run and one result per environment are recorded, then the
post-deploy hook runner is called once if every environment was written or found unchanged
(``app.services.hooks``), the lock is released and a deploy-finished event is emitted
(``app.services.deploy_events``).

A blocked or halted environment does not hold back the others of the same run; the post-deploy
command only runs when nothing was blocked, halted or failed. A dry run does the same checks
without the lock, a write or a backup and reports ``would_write`` for what a real run would write.

Decrypted content lives in memory for the duration of one environment and appears in no result,
log line, audit entry or exception message; messages are fixed text without host paths. The
database write transaction is never held across file work or the post-deploy command: the run is
committed before the hook runs, with its post-deploy state set to running and no finish time, and
updated in a second short transaction afterwards, so a page never reads a final state while the
command is still executing. ``app.services.deploy_recovery`` closes a run a crash left running.

Post-deploy output is scrubbed of the deployment's configuration values
(``app.services.scrub_values``) before it is stored on the run, written to the audit entry,
returned to the caller or shown.
"""

import asyncio
import os
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from time import monotonic

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics, telemetry
from app.core.crypto import CryptoError, CryptoService
from app.core.log import get_logger
from app.models import (
    Deployment,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DistOutcome,
    DriftState,
    Environment,
    EnvironmentVersion,
    HookType,
    PostDeployStatus,
    Service,
    ServiceHook,
)
from app.models.types import utc_now
from app.services.app_settings import load_stored_settings
from app.services.audit import AuditActor
from app.services.deploy_baseline import load_baselines
from app.services.deploy_events import (
    DeployFinished,
    DeployNotifier,
    EnvironmentResult,
    NoNotifier,
)
from app.services.deploy_lock import (
    DeployLockBusyError,
    DeployLockUnavailableError,
    hold_deploy_lock,
)
from app.services.deploy_policy import Action, Plan, drift_state, overall_status, plan_environment
from app.services.deploy_records import audit_refusal, audit_run, record_run, truncate
from app.services.deploy_recovery import INTERRUPTED_REASON
from app.services.deploy_types import (
    DeployOutcome,
    DeployRefusal,
    DeployRequest,
    DeployRequestError,
    EnvironmentOutcome,
)
from app.services.dist import DistError
from app.services.dist_check import DistCheck, evaluate_dist, record_dist_check
from app.services.editor import stored_version
from app.services.hook_runner import DockerHookRunner
from app.services.hooks import (
    POST_DEPLOY_OFF_REASON,
    HookRunner,
    PostDeployHook,
    PostDeployRequest,
    PostDeployResult,
)
from app.services.output_scrub import OutputScrubber
from app.services.paths import AllowedPaths, PathValidationError, load_allowed_paths
from app.services.permissions import AccessScope, Capability, permitted_environment_ids
from app.services.scrub_values import load_scrubber
from app.services.structure_lookup import service_with_deployment
from app.services.target_files import (
    BackupRequest,
    ChangeOwner,
    TargetError,
    TargetLocation,
    locate_target,
    parse_mode,
    read_target_file,
    resolve_ownership,
    write_target_file,
)

log = get_logger("deploy")

SERVICE_NOT_FOUND = "Service not found."
ENVIRONMENT_NOT_FOUND = "Environment not found."
VERSION_NOT_FOUND = "Version not found."
NOTHING_SELECTED = "No environments are selected."
LOCKED_MESSAGE = "Another deploy is running for this deployment folder. Try again when it ends."
LOCK_UNAVAILABLE_MESSAGE = "The deploy lock could not be taken."
NO_VERSION_MESSAGE = "No version is saved for this file."
DECRYPT_FAILED_MESSAGE = "The saved version cannot be read."
FILE_NO_LONGER_EXISTS_MESSAGE = "This file no longer exists."
INVALID_MODE_MESSAGE = "The configured file mode is invalid."
UNEXPECTED_MESSAGE = "The file could not be deployed because of an unexpected error."
UNCHANGED_MESSAGE = "The target already matches this version."
POST_DEPLOY_NOT_RUN_REASON = "Not run because an environment was not deployed."
POST_DEPLOY_ROOT_REASON = "Not run because the deployment is outside the allowed paths."
POST_DEPLOY_ERROR_REASON = "The post-deploy runner failed unexpectedly."
SCRUBBER_FAILED_REASON = "The command was not run because its output could not be protected."

_DEPLOYED = (DeployResultStatus.WRITTEN, DeployResultStatus.UNCHANGED)


@dataclass(frozen=True)
class _Context:
    """The loaded service, deployment, selected environments, versions and stored command."""

    service: Service
    deployment: Deployment
    environments: tuple[Environment, ...]
    versions: dict[int, EnvironmentVersion | None]
    hook: PostDeployHook | None


@dataclass(frozen=True)
class _Findings:
    """What the checks found for one environment, copied onto its outcome."""

    dist: DistOutcome | None = None
    drift: DriftState | None = None
    target_hash_before: str | None = None
    forced: bool = False
    check: DistCheck | None = None


@dataclass(frozen=True)
class _TargetReading:
    """The target as read for one environment and how it stands against what this app wrote."""

    current: bytes | None
    current_hash: str | None
    drift: DriftState


def _context_attributes(context: _Context) -> dict[str, object]:
    """The ids and names of the service and its ancestors, for a span."""
    return {
        telemetry.PROJECT_ID: context.deployment.project_id,
        telemetry.DEPLOYMENT_ID: context.deployment.id,
        telemetry.DEPLOYMENT_NAME: context.deployment.name,
        telemetry.SERVICE_ID: context.service.id,
        telemetry.SERVICE_NAME: context.service.name,
    }


def _outcome(
    environment: Environment,
    version: EnvironmentVersion | None,
    status: DeployResultStatus,
    message: str,
    findings: _Findings | None = None,
    *,
    written_hash: str | None = None,
    backup_path: str | None = None,
) -> EnvironmentOutcome:
    """An outcome of an environment with the findings of its checks."""
    found = findings or _Findings()
    return EnvironmentOutcome(
        environment_id=environment.id,
        name=environment.name,
        target_path=environment.target_path,
        version_id=version.id if version else None,
        version_number=version.number if version else None,
        status=status,
        message=message,
        dist=found.dist,
        drift=found.drift,
        target_hash_before=found.target_hash_before,
        written_hash=written_hash,
        backup_path=backup_path,
        forced=found.forced,
        check=found.check,
    )


@dataclass
class DeployEngine:
    """Runs deploys with the seams for the post-deploy command and the notification.

    ``lock_dir`` holds the lock files; ``change_owner`` is ``os.fchown`` and is a field so a test
    can stand in for the privilege the container user lacks.
    """

    crypto: CryptoService
    lock_dir: Path
    hook_runner: HookRunner = field(default_factory=DockerHookRunner)
    notifier: DeployNotifier = field(default_factory=NoNotifier)
    change_owner: ChangeOwner = os.fchown

    async def run(
        self,
        db: AsyncSession,
        scope: AccessScope,
        actor: AuditActor,
        request: DeployRequest,
    ) -> DeployOutcome:
        """Deploy (or dry run) the selected environments of one service.

        The environments are those in the request (else all) that ``scope`` may deploy. Raises
        ``DeployRequestError`` when nothing can be deployed from the request; every other
        outcome, including a refusal because the lock is held, is a returned ``DeployOutcome``.
        """
        context = await self._load_context(db, scope, request)
        attributes = {
            **_context_attributes(context),
            telemetry.ACTOR_TYPE: actor.type.value,
            telemetry.DRY_RUN: request.dry_run,
            telemetry.FORCED: request.force,
            telemetry.ENVIRONMENT_COUNT: len(context.environments),
        }
        with telemetry.span(telemetry.DEPLOY_SPAN, attributes) as active:
            started = monotonic()
            result = "error"
            try:
                outcome = await self._run_loaded(db, context, actor, request)
                result = "refused" if outcome.refusal is not None else outcome.status.value
                telemetry.annotate(active, {telemetry.RESULT: result})
            finally:
                if not request.dry_run:
                    metrics.record_deploy(result, monotonic() - started)
        return outcome

    async def _run_loaded(
        self,
        db: AsyncSession,
        context: _Context,
        actor: AuditActor,
        request: DeployRequest,
    ) -> DeployOutcome:
        """Run the deploy or the dry run of a loaded context and announce a recorded real run."""
        if request.dry_run:
            return await self._dry_run(db, context, actor, request)
        try:
            with hold_deploy_lock(self.lock_dir, context.deployment.root_path):
                outcome, event = await self._deploy_locked(db, context, actor, request)
        except DeployLockBusyError:
            return await self._refuse(db, context, actor, request, DeployRefusal.LOCKED)
        except DeployLockUnavailableError:
            return await self._refuse(db, context, actor, request, DeployRefusal.LOCK_UNAVAILABLE)
        await self._notify(event)
        return outcome

    async def _load_context(
        self, db: AsyncSession, scope: AccessScope, request: DeployRequest
    ) -> _Context:
        """The service and the environments to deploy, with the version of each."""
        chain = await service_with_deployment(db, request.service_id)
        permitted = await permitted_environment_ids(
            db, scope, Capability.DEPLOY, service_id=request.service_id
        )
        if chain is None or not permitted:
            raise DeployRequestError(SERVICE_NOT_FOUND)
        service, deployment = chain.service, chain.deployment
        if request.environment_ids is None:
            selected = permitted
        else:
            if not request.environment_ids:
                raise DeployRequestError(NOTHING_SELECTED)
            if not set(request.environment_ids) <= set(permitted):
                raise DeployRequestError(ENVIRONMENT_NOT_FOUND)
            selected = sorted(set(request.environment_ids))
        found = await db.execute(
            select(Environment).where(Environment.id.in_(selected)).order_by(Environment.name)
        )
        environments = tuple(found.scalars())
        versions: dict[int, EnvironmentVersion | None] = {}
        for environment in environments:
            number = request.versions.get(environment.id)
            version = await stored_version(db, environment.id, number)
            if number is not None and version is None:
                raise DeployRequestError(VERSION_NOT_FOUND)
            versions[environment.id] = version
        stored_hook = await db.scalar(
            select(ServiceHook).where(
                ServiceHook.service_id == service.id, ServiceHook.type == HookType.POST_DEPLOY
            )
        )
        hook = (
            PostDeployHook(
                commands=stored_hook.commands,
                working_directory=stored_hook.working_directory,
                timeout_seconds=stored_hook.timeout_seconds,
                run_only_if_changed=stored_hook.run_only_if_changed,
            )
            if stored_hook is not None
            else None
        )
        return _Context(service, deployment, environments, versions, hook)

    async def _load_baselines(self, db: AsyncSession, context: _Context) -> dict[int, str]:
        """The keyed hash of the last content this app wrote or found in each target."""
        baselines = await load_baselines(
            db, [environment.id for environment in context.environments]
        )
        return {environment_id: found.written_hash for environment_id, found in baselines.items()}

    async def _examine(
        self,
        db: AsyncSession,
        context: _Context,
        actor: AuditActor,
        request: DeployRequest,
    ) -> tuple[tuple[EnvironmentOutcome, ...], list[DistCheck], AllowedPaths]:
        """Run the checks and, for a real run, the writes of every selected environment.

        The allowed paths are loaded here, so they are fresh for this run.
        """
        allowed = await load_allowed_paths(db)
        baselines = await self._load_baselines(db, context)
        outcomes: list[EnvironmentOutcome] = []
        checks: list[DistCheck] = []
        for environment in context.environments:
            version = context.versions[environment.id]
            try:
                outcome, check = await self._process(
                    db,
                    context,
                    allowed,
                    actor,
                    request,
                    environment,
                    version,
                    baselines.get(environment.id),
                )
            except Exception as error:
                log.error("deploy of file %s failed: %s", environment.id, type(error).__name__)
                outcome = _outcome(
                    environment, version, DeployResultStatus.FAILED, UNEXPECTED_MESSAGE
                )
                check = None
            outcomes.append(outcome)
            if check is not None:
                checks.append(check)
        return tuple(outcomes), checks, allowed

    async def _process(
        self,
        db: AsyncSession,
        context: _Context,
        allowed: AllowedPaths,
        actor: AuditActor,
        request: DeployRequest,
        environment: Environment,
        version: EnvironmentVersion | None,
        baseline: str | None,
    ) -> tuple[EnvironmentOutcome, DistCheck | None]:
        """Validate, check and (for a real run) write one environment."""
        failed = DeployResultStatus.FAILED
        if version is None:
            return _outcome(environment, version, failed, NO_VERSION_MESSAGE), None
        try:
            data = self.crypto.decrypt_text(version.content_encrypted).encode("utf-8")
        except CryptoError:
            return _outcome(environment, version, failed, DECRYPT_FAILED_MESSAGE), None
        version_hash = self.crypto.content_hash(data)
        try:
            location = locate_target(allowed, context.deployment.root_path, environment.target_path)
        except TargetError as error:
            return _outcome(environment, version, failed, str(error)), None
        try:
            check = await evaluate_dist(
                db,
                self.crypto,
                environment.id,
                checked_by=actor.label,
                version_number=version.number,
            )
        except DistError:
            return _outcome(environment, version, failed, FILE_NO_LONGER_EXISTS_MESSAGE), None
        try:
            reading = await self._read_target(
                environment, location, allowed, version_hash, baseline, request.dry_run
            )
        except TargetError as error:
            unread = _outcome(
                environment, version, failed, str(error), _Findings(check.outcome, check=check)
            )
            return unread, check
        current, current_hash = reading.current, reading.current_hash
        deployment = context.deployment
        plan = plan_environment(
            drift=reading.drift,
            unchanged=current_hash == version_hash,
            check=check,
            drift_policy=environment.drift_policy or deployment.drift_policy,
            missing_policy=environment.missing_target_policy or deployment.missing_target_policy,
            force=request.force,
            backup_drifted=request.backup_drifted,
        )
        findings = _Findings(check.outcome, plan.drift, current_hash, plan.forced, check)
        if plan.action is Action.BLOCK:
            return _outcome(
                environment, version, DeployResultStatus.BLOCKED, plan.message, findings
            ), check
        if plan.action is Action.HALT:
            return _outcome(
                environment, version, DeployResultStatus.HALTED, plan.message, findings
            ), check
        if plan.action is Action.UNCHANGED:
            message = " ".join((UNCHANGED_MESSAGE, *plan.notes))
            return _outcome(
                environment,
                version,
                DeployResultStatus.UNCHANGED,
                message,
                findings,
                written_hash=version_hash,
            ), check
        if request.dry_run:
            verb = "Would create" if plan.created else "Would write"
            suffix = " The previous file would be backed up." if plan.backup else ""
            message = " ".join((f"{verb} this version.{suffix}", *plan.notes))
            return _outcome(
                environment, version, DeployResultStatus.WOULD_WRITE, message, findings
            ), check
        outcome = await self._write(
            context, allowed, environment, version, location, data, current, plan, findings
        )
        return outcome, check

    async def _read_target(
        self,
        environment: Environment,
        location: TargetLocation,
        allowed: AllowedPaths,
        version_hash: str,
        baseline: str | None,
        dry_run: bool,
    ) -> _TargetReading:
        """Read the target and decide its drift state, in a drift check span.

        Raises ``TargetError`` when the target cannot be read. A drifted target is counted.
        """
        with telemetry.span(
            telemetry.DRIFT_CHECK_SPAN, {telemetry.ENVIRONMENT_ID: environment.id}
        ) as active:
            current = await asyncio.to_thread(read_target_file, location, allowed)
            current_hash = self.crypto.content_hash(current) if current is not None else None
            drift = drift_state(current, current_hash, version_hash, baseline)
            telemetry.annotate(active, {telemetry.DRIFT_STATE: drift.value})
        if drift is DriftState.DRIFT:
            metrics.record_drift_detection("dry_run" if dry_run else "deploy")
        return _TargetReading(current, current_hash, drift)

    async def _write(
        self,
        context: _Context,
        allowed: AllowedPaths,
        environment: Environment,
        version: EnvironmentVersion,
        location: TargetLocation,
        data: bytes,
        current: bytes | None,
        plan: Plan,
        findings: _Findings,
    ) -> EnvironmentOutcome:
        """Back up if the policy says so and replace the target; a failure leaves it as it was."""
        deployment = context.deployment
        failed = DeployResultStatus.FAILED
        try:
            mode = parse_mode(environment.file_mode or deployment.file_mode)
        except ValueError:
            return _outcome(environment, version, failed, INVALID_MODE_MESSAGE, findings)
        try:
            ownership = resolve_ownership(
                environment.file_owner or deployment.file_owner,
                environment.file_group or deployment.file_group,
            )
            backup = (
                BackupRequest(current, utc_now().strftime("%Y%m%dT%H%M%SZ"))
                if plan.backup and current is not None
                else None
            )
            backup_name = await asyncio.to_thread(
                write_target_file,
                location,
                allowed,
                data,
                mode=mode,
                ownership=ownership,
                backup=backup,
                change_owner=self.change_owner,
            )
        except TargetError as error:
            return _outcome(environment, version, failed, str(error), findings)
        verb = "Created." if plan.created else "Written."
        suffix = " The previous file was backed up." if backup_name else ""
        backup_path = (
            (PurePosixPath(location.relative).parent / backup_name).as_posix()
            if backup_name
            else None
        )
        return _outcome(
            environment,
            version,
            DeployResultStatus.WRITTEN,
            " ".join((verb + suffix, *plan.notes)),
            findings,
            written_hash=self.crypto.content_hash(data),
            backup_path=backup_path,
        )

    async def _dry_run(
        self,
        db: AsyncSession,
        context: _Context,
        actor: AuditActor,
        request: DeployRequest,
    ) -> DeployOutcome:
        """Checks only: no lock, no write, no backup; recorded and audited as a dry run."""
        started_at = utc_now()
        outcomes, checks, _ = await self._examine(db, context, actor, request)
        status = overall_status(outcomes)
        post_deploy = PostDeployResult(PostDeployStatus.NONE)
        run = await record_run(
            db,
            service=context.service,
            deployment=context.deployment,
            actor=actor,
            request=request,
            started_at=started_at,
            status=status,
            outcomes=outcomes,
            post_deploy=post_deploy,
        )
        for check in checks:
            await record_dist_check(db, check, actor=actor)
        await audit_run(
            db,
            service=context.service,
            deployment=context.deployment,
            actor=actor,
            request=request,
            run=run,
            outcomes=outcomes,
        )
        await db.commit()
        return DeployOutcome(run.id, status, True, request.force, outcomes, post_deploy)

    async def _deploy_locked(
        self,
        db: AsyncSession,
        context: _Context,
        actor: AuditActor,
        request: DeployRequest,
    ) -> tuple[DeployOutcome, DeployFinished]:
        """The real deploy, called with the lock held; the lock covers the post-deploy step."""
        started_at = utc_now()
        outcomes, checks, allowed = await self._examine(db, context, actor, request)
        status = overall_status(outcomes)
        deployed = all(outcome.status in _DEPLOYED for outcome in outcomes)
        has_command = bool(context.hook and context.hook.commands.strip())
        switched_off = has_command and not (await load_stored_settings(db)).post_deploy.enabled
        if switched_off:
            post_deploy = PostDeployResult(PostDeployStatus.SKIPPED, reason=POST_DEPLOY_OFF_REASON)
        elif has_command and not deployed:
            post_deploy = PostDeployResult(
                PostDeployStatus.SKIPPED, reason=POST_DEPLOY_NOT_RUN_REASON
            )
        elif has_command:
            post_deploy = PostDeployResult(PostDeployStatus.RUNNING)
        else:
            post_deploy = PostDeployResult(PostDeployStatus.NONE)
        run = await record_run(
            db,
            service=context.service,
            deployment=context.deployment,
            actor=actor,
            request=request,
            started_at=started_at,
            status=status,
            outcomes=outcomes,
            post_deploy=post_deploy,
        )
        for check in checks:
            await record_dist_check(db, check, actor=actor)
        await db.commit()

        if deployed and not switched_off:
            try:
                post_deploy = await self._run_post_deploy(db, context, allowed, request, outcomes)
            except asyncio.CancelledError:
                await self._close_cancelled(db, context, actor, request, run, outcomes)
                raise
            if post_deploy.status is PostDeployStatus.FAILED:
                status = DeployStatus.POST_DEPLOY_FAILED
        run.status = status
        run.post_deploy_status = post_deploy.status
        run.post_deploy_exit_code = post_deploy.exit_code
        run.post_deploy_output = post_deploy.output
        run.post_deploy_reason = truncate(post_deploy.reason)
        run.finished_at = utc_now()
        await audit_run(
            db,
            service=context.service,
            deployment=context.deployment,
            actor=actor,
            request=request,
            run=run,
            outcomes=outcomes,
        )
        await db.commit()
        log.info("deploy run %s of service %s: %s", run.id, context.service.id, status.value)
        outcome = DeployOutcome(run.id, status, False, request.force, outcomes, post_deploy)
        return outcome, self._event(context, actor, request, run.id, outcome)

    async def _close_cancelled(
        self,
        db: AsyncSession,
        context: _Context,
        actor: AuditActor,
        request: DeployRequest,
        run: DeployRun,
        outcomes: tuple[EnvironmentOutcome, ...],
    ) -> None:
        """Record a deploy whose post-deploy step was cancelled as a failed post-deploy.

        The caller re-raises the cancellation. A failure to record is only logged: the next start
        of the app closes the run (``app.services.deploy_recovery``).
        """
        run.status = DeployStatus.POST_DEPLOY_FAILED
        run.post_deploy_status = PostDeployStatus.FAILED
        run.post_deploy_exit_code = None
        run.post_deploy_output = None
        run.post_deploy_reason = INTERRUPTED_REASON
        run.finished_at = utc_now()
        try:
            await audit_run(
                db,
                service=context.service,
                deployment=context.deployment,
                actor=actor,
                request=request,
                run=run,
                outcomes=outcomes,
            )
            await db.commit()
        except Exception as error:
            log.warning("a cancelled deploy could not be recorded: %s", type(error).__name__)

    @staticmethod
    def _event(
        context: _Context,
        actor: AuditActor,
        request: DeployRequest,
        run_id: int,
        outcome: DeployOutcome,
    ) -> DeployFinished:
        """The deploy-finished event for a recorded real run."""
        return DeployFinished(
            run_id=run_id,
            project_id=context.deployment.project_id,
            deployment_id=context.deployment.id,
            deployment_name=context.deployment.name,
            service_id=context.service.id,
            service_name=context.service.name,
            actor=actor.label,
            force=request.force,
            status=outcome.status,
            post_deploy=outcome.post_deploy.status,
            environments=tuple(
                EnvironmentResult(env.environment_id, env.name, env.version_number, env.status)
                for env in outcome.environments
            ),
        )

    async def _run_post_deploy(
        self,
        db: AsyncSession,
        context: _Context,
        allowed: AllowedPaths,
        request: DeployRequest,
        outcomes: tuple[EnvironmentOutcome, ...],
    ) -> PostDeployResult:
        """Call the hook runner once; a runner that fails counts as a failed post-deploy.

        When a command is stored, the values of the deployment's files are loaded first and
        removed from whatever the runner returns, so no output, whichever runner produced it,
        reaches the run record, the audit entry or a caller with a configuration value in it. If
        the values cannot be loaded the command is not run.
        """
        try:
            root = allowed.resolve(context.deployment.root_path)
        except PathValidationError:
            return PostDeployResult(PostDeployStatus.SKIPPED, reason=POST_DEPLOY_ROOT_REASON)
        scrubber = OutputScrubber()
        if context.hook is not None and context.hook.commands.strip():
            try:
                scrubber = await load_scrubber(
                    db, self.crypto, context.deployment.id, context.versions.values()
                )
            except Exception as error:
                log.error("output scrubbing values could not be loaded: %s", type(error).__name__)
                return PostDeployResult(PostDeployStatus.FAILED, reason=SCRUBBER_FAILED_REASON)
        hook_request = PostDeployRequest(
            service_id=context.service.id,
            deployment_id=context.deployment.id,
            deployment_root=root,
            hook=context.hook,
            changed=any(outcome.status is DeployResultStatus.WRITTEN for outcome in outcomes),
            force=request.force,
            allowed=allowed,
            scrubber=scrubber,
        )
        try:
            result = await self._call_hook_runner(context, hook_request)
        except Exception as error:
            log.error("post-deploy runner failed: %s", type(error).__name__)
            return PostDeployResult(PostDeployStatus.FAILED, reason=POST_DEPLOY_ERROR_REASON)
        if not scrubber:
            return result
        return replace(
            result,
            output=scrubber.scrub_text(result.output) if result.output else result.output,
            reason=scrubber.scrub_text(result.reason) if result.reason else result.reason,
        )

    async def _call_hook_runner(
        self, context: _Context, hook_request: PostDeployRequest
    ) -> PostDeployResult:
        """Call the hook runner; a stored command runs in a span and is counted by its result.

        The span holds ids, names, the result and the exit code; never the command, its output or
        its reason. A runner that raises counts as an error.
        """
        if context.hook is None or not context.hook.commands.strip():
            return await self.hook_runner.run_post_deploy(hook_request)
        started = monotonic()
        label = "error"
        with telemetry.span(telemetry.POST_DEPLOY_SPAN, _context_attributes(context)) as active:
            try:
                result = await self.hook_runner.run_post_deploy(hook_request)
                label = result.status.value
                telemetry.annotate(
                    active, {telemetry.RESULT: label, telemetry.EXIT_CODE: result.exit_code}
                )
                return result
            finally:
                metrics.record_post_deploy(label, monotonic() - started)

    async def _refuse(
        self,
        db: AsyncSession,
        context: _Context,
        actor: AuditActor,
        request: DeployRequest,
        refusal: DeployRefusal,
    ) -> DeployOutcome:
        """Audit a deploy that could not start because the lock is held or unusable."""
        await audit_refusal(
            db,
            service=context.service,
            deployment=context.deployment,
            actor=actor,
            request=request,
            environment_names=[environment.name for environment in context.environments],
            refusal=refusal,
        )
        await db.commit()
        message = LOCKED_MESSAGE if refusal is DeployRefusal.LOCKED else LOCK_UNAVAILABLE_MESSAGE
        return DeployOutcome(
            run_id=None,
            status=DeployStatus.FAILED,
            dry_run=False,
            force=request.force,
            environments=(),
            post_deploy=PostDeployResult(PostDeployStatus.NONE),
            refusal=refusal,
            message=message,
        )

    async def _notify(self, event: DeployFinished) -> None:
        """Emit the deploy-finished event; a notifier that fails never changes the outcome."""
        try:
            await self.notifier.deploy_finished(event)
        except Exception as error:
            log.error("deploy notification failed: %s", type(error).__name__)
