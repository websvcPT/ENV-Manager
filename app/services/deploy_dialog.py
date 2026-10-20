"""The deploy dialog: the pre-flight from a real dry run and the result of a deploy.

The pre-flight runs the deploy engine as a dry run over every environment of the service the
caller may deploy, so each row shows what a real run would decide (dist check, drift, policy)
without a second implementation of those rules. The result is the engine's own outcome turned
into banner, rows and post-deploy lines. Everything here holds names, version numbers, key
names, counts and fixed messages; the post-deploy output (already scrubbed and truncated by the
engine) is passed on only when the caller's group may read it, and the command text only when
the group may read that.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DistMissingPolicy,
    DistOutcome,
    DriftState,
    PostDeployStatus,
)
from app.services.app_settings import StoredSettings, load_stored_settings
from app.services.audit import AuditActor
from app.services.deploy import ENVIRONMENT_NOT_FOUND, SERVICE_NOT_FOUND, DeployEngine
from app.services.deploy_types import (
    DeployOutcome,
    DeployRequest,
    DeployRequestError,
    EnvironmentOutcome,
)
from app.services.dist import DistSkipReason
from app.services.docker_cli import DockerCli
from app.services.hook_runner import DOCKER_UNAVAILABLE_REASON, NOT_CHANGED_REASON
from app.services.hooks import POST_DEPLOY_OFF_REASON, PostDeployHook
from app.services.permissions import AccessScope
from app.services.service_detail import (
    STATUS_NEVER,
    STATUS_PENDING,
    EnvironmentRow,
    ServiceHeader,
    load_environment_rows,
)

NO_COMMAND_TEXT = "This service has no post-deploy command."
NO_SELECTION_TEXT = "No environment is selected. Choose the environments to deploy."
NOT_DEPLOYED_REASON = "Not run because an environment would not be written."
MISSING_NAMES_SHOWN = 6
_PRESELECTED = frozenset({STATUS_PENDING, STATUS_NEVER})
_DIST_SKIPPED = {
    DistSkipReason.ENVIRONMENT_NOT_ENV_FORMAT: "Skipped, not env format",
    DistSkipReason.DIST_NOT_ENV_FORMAT: "Skipped, dist file is not env format",
    DistSkipReason.NO_DIST_LINKED: "Skipped, no dist file linked",
    DistSkipReason.NOT_APPLICABLE: "Skipped, not applicable to a certificate",
}
_DRIFT_TEXT = {
    DriftState.IN_SYNC: "Drift: None",
    DriftState.DRIFT: "Drift: Target changed outside the app",
    DriftState.MISSING: "Drift: Target missing",
}
_STOPPED = (
    DeployResultStatus.BLOCKED,
    DeployResultStatus.HALTED,
    DeployResultStatus.FAILED,
)


def plural(count: int, noun: str) -> str:
    """``1 environment`` or ``2 environments``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(frozen=True)
class PreflightRow:
    """One environment of the pre-flight table.

    ``version`` is the version the dry run used (and a deploy will write), ``newest`` the newest
    saved version. ``badge`` is a badge key (proceed, warnout, halt, block, unchanged, failed),
    ``badge_label`` overrides its text and ``problem`` is the fixed message of a failed row.
    """

    environment_id: int
    name: str
    version: int | None
    newest: int | None
    dist_text: str
    drift_text: str
    badge: str
    badge_label: str | None
    problem: str | None
    selected: bool


@dataclass(frozen=True)
class PostDeployPreview:
    """Whether the post-deploy command will run after the selected environments.

    ``state`` is ``run``, ``skipped``, ``none`` (no command) or ``idle`` (nothing selected).
    ``command`` is the stored text and is set for the superadmin only.
    """

    state: str
    reason: str | None = None
    command: str | None = None


@dataclass(frozen=True)
class Preflight:
    """Everything step one of the dialog shows."""

    service: ServiceHeader
    rows: tuple[PreflightRow, ...]
    selected: int
    summary: str
    preview: PostDeployPreview
    force: bool
    older_version: str | None


def _dist_text(outcome: EnvironmentOutcome) -> str:
    """The dist column of a row, from the check the dry run made."""
    check = outcome.check
    if check is None:
        return "Not checked"
    policy = "block" if check.policy is DistMissingPolicy.BLOCK else "warn"
    if check.outcome is DistOutcome.PASSED:
        return "OK, 0 missing keys"
    if check.outcome in (DistOutcome.WARNING, DistOutcome.BLOCKED):
        return f"{plural(_missing_count(outcome), 'missing key')}, policy: {policy}"
    if check.outcome is DistOutcome.SKIPPED and check.report and check.report.skipped:
        return _DIST_SKIPPED[check.report.skipped]
    return f"Check failed. {check.problem_message or ''}".strip()


def _drift_text(outcome: EnvironmentOutcome) -> str:
    """The drift column of a row."""
    if outcome.drift is None:
        return "Drift: Not checked"
    return _DRIFT_TEXT[outcome.drift]


def _missing_count(outcome: EnvironmentOutcome) -> int:
    """How many dist keys the check found missing."""
    check = outcome.check
    return len(check.report.missing) if check and check.report else 0


def _warns(outcome: EnvironmentOutcome) -> bool:
    """Whether a row that would be written does so with a warning or past a policy."""
    check = outcome.check
    if outcome.forced:
        return True
    if check is None:
        return False
    return check.outcome is DistOutcome.WARNING or (
        check.outcome is DistOutcome.FAILED and check.policy is DistMissingPolicy.WARN
    )


def _row_badge(outcome: EnvironmentOutcome) -> tuple[str, str | None]:
    """The badge key and optional label of a dry-run outcome."""
    status = outcome.status
    if status is DeployResultStatus.WOULD_WRITE:
        return ("warnout", None) if _warns(outcome) else ("proceed", None)
    if status is DeployResultStatus.UNCHANGED:
        return "unchanged", None
    if status is DeployResultStatus.BLOCKED:
        return "block", None
    if status is DeployResultStatus.HALTED:
        return "halt", None
    return "failed", "Will fail"


def _summary(outcomes: Sequence[EnvironmentOutcome]) -> str:
    """The dry-run line: what the selected environments would do."""
    if not outcomes:
        return NO_SELECTION_TEXT
    writes = [item for item in outcomes if item.status is DeployResultStatus.WOULD_WRITE]
    parts = [
        f"{plural(len(writes), 'environment')} would be written",
        plural(sum(1 for item in writes if _warns(item)), "warning"),
        f"{sum(1 for item in outcomes if item.status is DeployResultStatus.BLOCKED)} blocked",
    ]
    for status, label in (
        (DeployResultStatus.HALTED, "halted"),
        (DeployResultStatus.FAILED, "would fail"),
        (DeployResultStatus.UNCHANGED, "unchanged"),
    ):
        count = sum(1 for item in outcomes if item.status is status)
        if count:
            parts.append(f"{count} {label}")
    return f"Dry run: {', '.join(parts)}. Nothing has been changed yet."


def _is_older(row: PreflightRow) -> bool:
    """Whether the row deploys a version older than the newest saved one."""
    return row.version is not None and row.newest is not None and row.version < row.newest


async def preview_post_deploy(
    cli: DockerCli,
    header: ServiceHeader,
    selected: Sequence[EnvironmentOutcome],
    *,
    force: bool,
    hook: PostDeployHook | None,
    post_deploy_enabled: bool,
) -> PostDeployPreview:
    """Whether the stored command would run once after the selected environments.

    ``hook`` is the command the dry run was made with and ``post_deploy_enabled`` the switch of
    the Settings, both read once for the request. The same conditions the engine and the runner
    apply at deploy time: the feature switched on, nothing stopped (blocked, halted or failed),
    something would change unless the command runs always or the deploy is forced, and Docker
    usable. Docker is probed only when everything else lets the command run.
    """
    if hook is None or not hook.commands.strip():
        return PostDeployPreview("none", NO_COMMAND_TEXT)
    command = hook.commands if header.can_view_command else None
    if not selected:
        return PostDeployPreview("idle", command=command)
    if not post_deploy_enabled:
        return PostDeployPreview("skipped", POST_DEPLOY_OFF_REASON, command)
    if any(item.status in _STOPPED for item in selected):
        return PostDeployPreview("skipped", NOT_DEPLOYED_REASON, command)
    changes = any(item.status is DeployResultStatus.WOULD_WRITE for item in selected)
    if hook.run_only_if_changed and not (changes or force):
        return PostDeployPreview("skipped", NOT_CHANGED_REASON, command)
    status = await cli.status()
    if not status.available:
        return PostDeployPreview("skipped", status.reason or DOCKER_UNAVAILABLE_REASON, command)
    return PostDeployPreview("run", command=command)


def _default_selection(rows: Sequence[EnvironmentRow], pinned: dict[int, int]) -> set[int]:
    """Pending and never deployed files; for a chosen version only that file."""
    if pinned:
        return set(pinned)
    return {row.id for row in rows if row.status in _PRESELECTED}


async def run_preflight(
    db: AsyncSession,
    engine: DeployEngine,
    cli: DockerCli,
    scope: AccessScope,
    actor: AuditActor,
    header: ServiceHeader,
    *,
    selected_ids: set[int] | None,
    versions: dict[int, int],
    force: bool,
    stored: StoredSettings | None = None,
) -> Preflight:
    """Dry run every environment the caller may deploy and describe what a deploy would do.

    ``selected_ids`` is the choice the person made (None: pending and never deployed files, or
    the one file of a chosen version). ``versions`` pins version numbers; the rest use the newest.
    ``stored`` is the settings the request already read; without it they are read here.
    Raises ``DeployRequestError`` when the caller may deploy nothing here or a version is unknown.
    """
    status_rows = await load_environment_rows(db, scope, header)
    permitted = {row.id for row in status_rows if row.can_check_dist}
    if not permitted:
        raise DeployRequestError(SERVICE_NOT_FOUND)
    pinned = {key: value for key, value in versions.items() if key in permitted}
    newest = {row.id: row.current for row in status_rows}
    chosen = (
        {item for item in selected_ids if item in permitted}
        if selected_ids is not None
        else _default_selection(status_rows, pinned) & permitted
    )
    outcome = await engine.run(
        db,
        scope,
        actor,
        DeployRequest(service_id=header.id, versions=pinned, dry_run=True, force=force),
    )
    rows: list[PreflightRow] = []
    for item in outcome.environments:
        badge, label = _row_badge(item)
        rows.append(
            PreflightRow(
                environment_id=item.environment_id,
                name=item.name,
                version=item.version_number,
                newest=newest.get(item.environment_id),
                dist_text=_dist_text(item),
                drift_text=_drift_text(item),
                badge=badge,
                badge_label=label,
                problem=item.message if item.status is DeployResultStatus.FAILED else None,
                selected=item.environment_id in chosen,
            )
        )
    selected_outcomes = [item for item in outcome.environments if item.environment_id in chosen]
    older = [row for row in rows if row.selected and _is_older(row)]
    settings = stored if stored is not None else await load_stored_settings(db)
    return Preflight(
        service=header,
        rows=tuple(rows),
        selected=len(selected_outcomes),
        summary=_summary(selected_outcomes),
        preview=await preview_post_deploy(
            cli,
            header,
            selected_outcomes,
            force=force,
            hook=outcome.hook,
            post_deploy_enabled=settings.post_deploy.enabled,
        ),
        force=force,
        older_version=(
            f"Deploying version {older[0].version} of {older[0].name}, which is not the newest "
            f"version (v{older[0].newest})."
            if older
            else None
        ),
    )


async def selected_names(
    db: AsyncSession, scope: AccessScope, header: ServiceHeader, selected_ids: set[int]
) -> list[str]:
    """The names of the chosen environments, for the force confirmation.

    Raises ``DeployRequestError`` when one of them is not an environment the caller may deploy.
    """
    rows = await load_environment_rows(db, scope, header)
    permitted = {row.id for row in rows if row.can_check_dist}
    if not permitted:
        raise DeployRequestError(SERVICE_NOT_FOUND)
    if not selected_ids <= permitted:
        raise DeployRequestError(ENVIRONMENT_NOT_FOUND)
    return [row.name for row in rows if row.id in selected_ids]


@dataclass(frozen=True)
class ResultRow:
    """One environment of the result table; ``drift`` marks a row the drift view can open."""

    environment_id: int
    name: str
    version: int | None
    badge: str
    label: str | None
    detail: str
    drift: bool


@dataclass(frozen=True)
class PostDeployView:
    """The post-deploy block of the result: badge key, one line and the scrubbed output."""

    badge: str
    text: str
    output: str | None


@dataclass(frozen=True)
class DeployResultView:
    """Everything step two of the dialog shows.

    ``tone`` is ok, warn or err. ``drift_environment`` and ``dist_environment`` are the first
    halted and blocked environments, for the footer links. ``audit_entry_id`` is None when the
    deploy never started.
    """

    tone: str
    icon: str
    title: str
    message: str
    rows: tuple[ResultRow, ...]
    post_deploy: PostDeployView
    audit_entry_id: int | None
    drift_environment: int | None
    dist_environment: int | None
    retry: bool


def _missing_names(outcome: EnvironmentOutcome) -> str:
    """The missing dist key names of a blocked row, a few at most."""
    check = outcome.check
    names = list(check.report.missing) if check and check.report else []
    shown = ", ".join(names[:MISSING_NAMES_SHOWN])
    rest = len(names) - MISSING_NAMES_SHOWN
    return f"{shown} and {rest} more" if rest > 0 else shown


def _written_detail(outcome: EnvironmentOutcome) -> str:
    """The detail of a written or unchanged row."""
    if outcome.status is DeployResultStatus.UNCHANGED:
        base = "Already matches this version"
    elif outcome.drift is DriftState.MISSING:
        base = f"Created {outcome.target_path}"
    else:
        base = f"Written to {outcome.target_path}"
    extras = []
    if outcome.backup_path:
        extras.append("previous file backed up")
    if outcome.check and outcome.check.outcome is DistOutcome.WARNING and outcome.check.report:
        extras.append(plural(len(outcome.check.report.missing), "dist warning"))
    if outcome.forced:
        extras.append("forced past the checks")
    return ", ".join([base, *extras])


def _result_row(outcome: EnvironmentOutcome) -> ResultRow:
    """The result table row of an outcome."""
    status = outcome.status
    if status in (DeployResultStatus.WRITTEN, DeployResultStatus.UNCHANGED):
        badge = "success" if status is DeployResultStatus.WRITTEN else "unchanged"
        detail = _written_detail(outcome)
    elif status is DeployResultStatus.BLOCKED:
        badge = "blocked"
        check = outcome.check
        detail = (
            f"Missing {_missing_names(outcome)}"
            if check and check.outcome is DistOutcome.BLOCKED
            else outcome.message
        )
    elif status is DeployResultStatus.HALTED:
        badge, detail = "halted", outcome.message
    else:
        badge, detail = "failed", outcome.message
    return ResultRow(
        environment_id=outcome.environment_id,
        name=outcome.name,
        version=outcome.version_number,
        badge=badge,
        label=None,
        detail=detail,
        drift=status is DeployResultStatus.HALTED and outcome.drift is DriftState.DRIFT,
    )


def _post_deploy_sentence(outcome: DeployOutcome) -> str:
    """One sentence on the post-deploy command for the banner of a clean deploy."""
    result = outcome.post_deploy
    if result.status is PostDeployStatus.RAN:
        return "The post-deploy command ran once."
    if result.status is PostDeployStatus.SKIPPED:
        return f"The post-deploy command was skipped. {result.reason or ''}".strip()
    return "No post-deploy command is set."


def _others_written(written: int) -> str:
    """The sentence that says the rest of a run was written."""
    if written == 0:
        return "Nothing was written."
    return f"{plural(written, 'other environment')} written."


def _names(outcomes: Sequence[EnvironmentOutcome]) -> str:
    """File names joined for a sentence."""
    return ", ".join(item.name for item in outcomes)


def _banner(outcome: DeployOutcome, show_exit_code: bool) -> tuple[str, str, str, str]:
    """Tone, icon, title and message of the result banner."""
    written = sum(1 for item in outcome.environments if item.status is DeployResultStatus.WRITTEN)
    of_status = {
        status: [item for item in outcome.environments if item.status is status]
        for status in DeployResultStatus
    }
    if outcome.refusal is not None:
        return "err", "close", "Failed", outcome.message or "The deploy did not start."
    if outcome.status is DeployStatus.SUCCESS:
        unchanged = len(of_status[DeployResultStatus.UNCHANGED])
        head = (
            f"{plural(written, 'environment')} written."
            if written
            else f"Nothing needed writing: {plural(unchanged, 'environment')} already matched."
        )
        return "ok", "check", "Success", f"{head} {_post_deploy_sentence(outcome)}"
    if outcome.status is DeployStatus.POST_DEPLOY_FAILED:
        code = outcome.post_deploy.exit_code
        failure = (
            f"The post-deploy command exited with code {code}"
            if code is not None and show_exit_code
            else "The post-deploy command failed"
        )
        return (
            "warn",
            "warning",
            "Deployed, post-deploy failed",
            f"{plural(written, 'environment')} written. {failure}, so the workload may still "
            "run the old files.",
        )
    if outcome.status is DeployStatus.FAILED:
        failed = of_status[DeployResultStatus.FAILED]
        head = (
            f"{failed[0].name} could not be written. {failed[0].message}"
            if len(failed) == 1
            else f"{plural(len(failed), 'environment')} failed."
        )
        return "err", "close", "Failed", f"{head} {_others_written(written)}"
    if outcome.status is DeployStatus.BLOCKED:
        blocked = of_status[DeployResultStatus.BLOCKED]
        by_dist = [
            item for item in blocked if item.check and item.check.outcome is DistOutcome.BLOCKED
        ]
        if len(blocked) == 1 and by_dist:
            head = (
                f"{blocked[0].name} is missing {plural(_missing_count(blocked[0]), 'key')} "
                "from its dist file. Policy is block."
            )
        else:
            head = f"{_names(blocked)}: the dist check blocked the deploy. Policy is block."
        title = "Blocked: missing keys" if by_dist else "Blocked"
        return "err", "ban", title, f"{head} {_others_written(written)}"
    halted = of_status[DeployResultStatus.HALTED]
    drifted = [item for item in halted if item.drift is DriftState.DRIFT]
    if drifted and len(drifted) == len(halted):
        subject = "was" if len(drifted) == 1 else "were"
        head = (
            f"{_names(drifted)} changed outside the app. The drift policy is halt, so "
            f"{'it' if subject == 'was' else 'they'} {subject} not written."
        )
        title = "Halted: drift"
    else:
        head = f"{_names(halted)} halted by policy and not written."
        title = "Halted"
    return "warn", "pause", title, f"{head} {_others_written(written)}" if written else head


def _post_deploy_view(outcome: DeployOutcome, header: ServiceHeader) -> PostDeployView:
    """The post-deploy block; output only for the groups that may read it."""
    result = outcome.post_deploy
    show = header.can_view_output
    if result.status is PostDeployStatus.RAN:
        code = f" · exit code {result.exit_code}" if show and result.exit_code is not None else ""
        return PostDeployView("ran", f"Ran once{code}", result.output if show else None)
    if result.status is PostDeployStatus.FAILED:
        pieces = ["Failed"]
        if show and result.exit_code is not None:
            pieces.append(f"exit code {result.exit_code}")
        if result.reason and result.reason != f"The command exited with code {result.exit_code}.":
            pieces.append(result.reason)
        return PostDeployView("failed", " · ".join(pieces), result.output if show else None)
    if result.status is PostDeployStatus.SKIPPED:
        return PostDeployView("skipped", result.reason or "Skipped.", None)
    return PostDeployView("none", NO_COMMAND_TEXT, None)


async def build_result(
    db: AsyncSession, header: ServiceHeader, outcome: DeployOutcome
) -> DeployResultView:
    """The result step of the dialog for the outcome of a real deploy."""
    tone, icon, title, message = _banner(outcome, header.can_view_output)
    rows = tuple(_result_row(item) for item in outcome.environments)
    audit_entry_id = None
    if outcome.run_id is not None:
        run = await db.get(DeployRun, outcome.run_id)
        audit_entry_id = run.audit_entry_id if run is not None else None
    drift = next((row.environment_id for row in rows if row.drift), None)
    dist = next(
        (
            item.environment_id
            for item in outcome.environments
            if item.status is DeployResultStatus.BLOCKED
        ),
        None,
    )
    return DeployResultView(
        tone=tone,
        icon=icon,
        title=title,
        message=message,
        rows=rows,
        post_deploy=_post_deploy_view(outcome, header),
        audit_entry_id=audit_entry_id,
        drift_environment=drift,
        dist_environment=dist,
        retry=outcome.status is DeployStatus.FAILED,
    )
