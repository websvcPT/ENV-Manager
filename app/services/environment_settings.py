"""Environment settings edit, superadmin only and audited.

The settings are the target and dist paths, the file mode, owner and group, and the three policy
overrides the deploy engine and the dist check read before the deployment's own policies. The
paths follow the rules of the add environment form. A target path change leaves the file at the
old path alone and makes the file count as never deployed at the new one.
"""

from dataclasses import dataclass, fields
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Deployment,
    DistMissingPolicy,
    DriftPolicy,
    Environment,
    EnvironmentVersion,
    MissingTargetPolicy,
    User,
)
from app.services.audit import AuditActor, AuditedEnvironment, record_environment_audit
from app.services.deploy_baseline import load_baselines
from app.services.deployments import FILE_MODE_PATTERN, parse_account
from app.services.environments import (
    EnvironmentFormError,
    relative_path,
    target_in_use,
)
from app.services.paths import AllowedPaths

NOT_ACKNOWLEDGED = (
    "Confirm that the file counts as not deployed at the new path, or keep the current path."
)


@dataclass(frozen=True)
class EnvironmentSettingsForm:
    """The submitted settings as typed. An empty policy means the deployment's own policy.

    ``acknowledge_move`` is the confirmation a changed target path of a deployed file needs.
    """

    target_path: str = ""
    dist_path: str = ""
    file_mode: str = ""
    file_owner: str = ""
    file_group: str = ""
    dist_missing_policy: str = ""
    drift_policy: str = ""
    missing_target_policy: str = ""
    acknowledge_move: bool = False

    @classmethod
    def from_environment(cls, environment: Environment) -> EnvironmentSettingsForm:
        """The form as it reads for the stored settings."""
        return cls(
            target_path=environment.target_path,
            dist_path=environment.dist_path or "",
            file_mode=environment.file_mode or "",
            file_owner=environment.file_owner or "",
            file_group=environment.file_group or "",
            dist_missing_policy=_policy_text(environment.dist_missing_policy),
            drift_policy=_policy_text(environment.drift_policy),
            missing_target_policy=_policy_text(environment.missing_target_policy),
        )


def _policy_text(policy: StrEnum | None) -> str:
    """The stored policy value, empty for the deployment default."""
    return policy.value if policy is not None else ""


@dataclass(frozen=True)
class CleanSettings:
    """Validated settings ready to store; None means the deployment's own value."""

    target_path: str
    dist_path: str | None
    file_mode: str | None
    file_owner: str | None
    file_group: str | None
    dist_missing_policy: DistMissingPolicy | None
    drift_policy: DriftPolicy | None
    missing_target_policy: MissingTargetPolicy | None


@dataclass(frozen=True)
class DeployedState:
    """What the app last wrote to the current target path of a file.

    ``version`` is the number of that version, None when the version row is gone.
    """

    version: int | None


async def deployed_state(db: AsyncSession, environment: Environment) -> DeployedState | None:
    """The last write to the environment's current target path, None when there was none."""
    baseline = (await load_baselines(db, [environment.id])).get(environment.id)
    if baseline is None:
        return None
    number = None
    if baseline.version_id is not None:
        number = (
            await db.execute(
                select(EnvironmentVersion.number).where(
                    EnvironmentVersion.id == baseline.version_id
                )
            )
        ).scalar_one_or_none()
    return DeployedState(number)


def _policy[ChoiceT: StrEnum](
    choice: type[ChoiceT], raw: str, key: str, errors: dict[str, str]
) -> ChoiceT | None:
    """The override for a submitted value, None for empty (the deployment's policy)."""
    text = raw.strip()
    if not text:
        return None
    try:
        return choice(text)
    except ValueError:
        errors[key] = "Choose one of the listed policies."
        return None


async def validate_settings(
    db: AsyncSession,
    allowed: AllowedPaths,
    deployment: Deployment,
    environment: Environment,
    form: EnvironmentSettingsForm,
) -> CleanSettings:
    """Check every submitted value and return the clean ones, or raise with all the problems.

    ``allowed`` must be freshly loaded by the caller.
    """
    errors: dict[str, str] = {}
    target_path: str | None = None
    if not form.target_path.strip():
        errors["target_path"] = "Enter a target path."
    else:
        target_path, _, problem = relative_path(
            allowed, deployment.root_path, form.target_path, "Target"
        )
        if problem:
            errors["target_path"] = problem
        elif target_path and await target_in_use(
            db, deployment.id, target_path, ignore_id=environment.id
        ):
            target_path = None
            errors["target_path"] = "Another file of this deployment already uses this target path."
        elif (
            target_path
            and target_path != environment.target_path
            and not form.acknowledge_move
            and await deployed_state(db, environment) is not None
        ):
            errors["acknowledge_move"] = NOT_ACKNOWLEDGED

    dist_path: str | None = None
    if form.dist_path.strip():
        dist_path, _, problem = relative_path(allowed, deployment.root_path, form.dist_path, "Dist")
        if problem:
            errors["dist_path"] = problem
        elif dist_path is not None and dist_path == target_path:
            dist_path = None
            errors["dist_path"] = "The dist file must be a different file from the target."

    mode = form.file_mode.strip()
    file_mode: str | None = None
    if mode and FILE_MODE_PATTERN.fullmatch(mode) is None:
        errors["file_mode"] = "Use an octal mode such as 0600."
    elif mode:
        file_mode = mode.zfill(4)
    owner = parse_account(form.file_owner, "Owner", errors, "file_owner")
    group = parse_account(form.file_group, "Group", errors, "file_group")

    dist_missing = _policy(
        DistMissingPolicy, form.dist_missing_policy, "dist_missing_policy", errors
    )
    drift = _policy(DriftPolicy, form.drift_policy, "drift_policy", errors)
    missing_target = _policy(
        MissingTargetPolicy, form.missing_target_policy, "missing_target_policy", errors
    )

    if errors or target_path is None:
        raise EnvironmentFormError(errors)
    return CleanSettings(
        target_path=target_path,
        dist_path=dist_path,
        file_mode=file_mode,
        file_owner=owner,
        file_group=group,
        dist_missing_policy=dist_missing,
        drift_policy=drift,
        missing_target_policy=missing_target,
    )


def _audit_value(value: object) -> str | None:
    """A setting as the audit entry keeps it: plain text, None for the default."""
    if value is None:
        return None
    return value.value if isinstance(value, StrEnum) else str(value)


async def update_settings(
    db: AsyncSession,
    environment: Environment,
    reference: AuditedEnvironment,
    clean: CleanSettings,
    *,
    actor: User,
    ip_address: str | None = None,
) -> list[str]:
    """Apply validated settings and audit what changed; returns the names of the changed ones.

    ``reference`` names the file and its ancestors for the audit scope. The entry carries the
    old and new value of each setting, never any file content.
    """
    changes: dict[str, dict[str, str | None]] = {}
    for item in fields(clean):
        old = getattr(environment, item.name)
        new = getattr(clean, item.name)
        if old != new:
            changes[item.name] = {"from": _audit_value(old), "to": _audit_value(new)}
            setattr(environment, item.name, new)
    if not changes:
        return []
    await record_environment_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        reference,
        action="environment.update",
        details={"changed": list(changes), "changes": changes},
    )
    await db.commit()
    return list(changes)
