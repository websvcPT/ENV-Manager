"""Deployment structure: root path checks and superadmin-only create and update.

The root path is only ever probed on the filesystem when it lies inside the allowed paths. A
path outside them answers with that single reason, whatever exists there, so the form cannot be
used to explore the host beyond the list.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditActorType,
    AuditResult,
    Deployment,
    DistMissingPolicy,
    DriftPolicy,
    MissingTargetPolicy,
    Project,
    User,
)
from app.services.audit import record_audit
from app.services.paths import (
    AllowedPaths,
    PathValidationError,
    is_within,
    normalize_path,
    path_status,
)
from app.services.projects import MAX_PROJECT_NAME_LENGTH, PROJECT_NAME_PATTERN

FILE_MODE_PATTERN = re.compile(r"0?[0-7]{3}")
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
MAX_ACCOUNT_LENGTH = 64
DEFAULT_FILE_MODE = "0600"


@dataclass(frozen=True)
class CheckLine:
    """One line of the root path validation: a passed check or the reason it failed."""

    ok: bool
    text: str


@dataclass(frozen=True)
class RootCheck:
    """The outcome of checking a typed root path.

    ``real_path`` is set only when every check passed; it is what gets stored. ``lines`` is empty
    for an empty input, which shows the neutral hint instead.
    """

    lines: tuple[CheckLine, ...]
    real_path: Path | None = None

    @property
    def ok(self) -> bool:
        """Whether the path may be used as a deployment root."""
        return self.real_path is not None

    @property
    def message(self) -> str | None:
        """The first failed line, used as the field error."""
        return next((line.text for line in self.lines if not line.ok), None)


def check_root_path(
    allowed: AllowedPaths, raw: str, *, mount_points: Iterable[Path] | None = None
) -> RootCheck:
    """Check a typed root path against the allowed paths and, inside them, the filesystem.

    The allowed paths are matched on the typed path before anything is read from disk, and a
    path whose real location leaves them (a symlink, a loop) gets the same single outside
    answer, so existence is never reported for anything outside the list.
    """
    text = raw.strip()
    if not text:
        return RootCheck(lines=())
    roots = allowed.usable_roots()
    if not roots:
        return RootCheck(
            lines=(CheckLine(False, "No allowed paths are configured. Add one in Settings first."),)
        )
    try:
        typed = normalize_path(text)
    except PathValidationError as error:
        reason = str(error)
        return RootCheck(lines=(CheckLine(False, reason[0].upper() + reason[1:] + "."),))
    outside = RootCheck(
        lines=(
            CheckLine(
                False,
                "Path is outside the allowed paths ("
                + ", ".join(str(root) for root in roots)
                + ").",
            ),
        )
    )
    if not any(is_within(typed, root) for root in roots):
        return outside
    try:
        real_path = allowed.resolve(typed)
    except PathValidationError:
        return outside
    container = next(root for root in roots if is_within(real_path, root))
    lines = [CheckLine(True, f"Inside allowed path {container}")]
    if not real_path.exists():
        lines.append(CheckLine(False, "Path does not exist."))
    elif not real_path.is_dir():
        lines.append(CheckLine(False, "Path is not a directory."))
    else:
        status = path_status(real_path, mount_points)
        if not status.mounted:
            lines.append(CheckLine(False, "Path is not inside a mounted host directory."))
        if not status.writable:
            lines.append(CheckLine(False, "Path is not writable by the app."))
        if status.mounted and status.writable:
            lines.append(CheckLine(True, "Exists, mounted and writable"))
    if all(line.ok for line in lines):
        return RootCheck(lines=tuple(lines), real_path=real_path)
    return RootCheck(lines=tuple(lines))


@dataclass(frozen=True)
class DeploymentForm:
    """The submitted form values, as typed."""

    name: str = ""
    root: str = ""
    dist_missing_policy: str = DistMissingPolicy.BLOCK.value
    drift_policy: str = DriftPolicy.HALT.value
    missing_target_policy: str = MissingTargetPolicy.CREATE.value
    file_mode: str = DEFAULT_FILE_MODE
    file_owner: str = ""
    file_group: str = ""

    @classmethod
    def from_deployment(cls, deployment: Deployment) -> DeploymentForm:
        """The form values of a stored deployment."""
        return cls(
            name=deployment.name,
            root=deployment.root_path,
            dist_missing_policy=deployment.dist_missing_policy.value,
            drift_policy=deployment.drift_policy.value,
            missing_target_policy=deployment.missing_target_policy.value,
            file_mode=deployment.file_mode,
            file_owner=deployment.file_owner or "",
            file_group=deployment.file_group or "",
        )


class DeploymentFormError(Exception):
    """The submitted values were refused; ``errors`` maps a field name to its message."""

    def __init__(self, errors: dict[str, str], root_check: RootCheck | None = None) -> None:
        """Hold the field messages and, when the root was checked, the full check lines."""
        super().__init__("; ".join(errors.values()))
        self.errors = errors
        self.root_check = root_check


@dataclass(frozen=True)
class CleanDeployment:
    """Validated values ready to store. ``root_path`` is the real path."""

    name: str
    root_path: str
    dist_missing_policy: DistMissingPolicy
    drift_policy: DriftPolicy
    missing_target_policy: MissingTargetPolicy
    file_mode: str
    file_owner: str | None
    file_group: str | None
    root_check: RootCheck = field(compare=False)


def _enum_value[ChoiceT: (DistMissingPolicy, DriftPolicy, MissingTargetPolicy)](
    choice: type[ChoiceT], raw: str
) -> ChoiceT | None:
    """The enum member for a submitted value, or None when it is not one of them."""
    try:
        return choice(raw)
    except ValueError:
        return None


def _account(raw: str, label: str, errors: dict[str, str], key: str) -> str | None:
    """A trimmed owner or group name, None when empty, with an error recorded when invalid."""
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_ACCOUNT_LENGTH or ACCOUNT_PATTERN.fullmatch(value) is None:
        errors[key] = f"{label} must be a user or group name or a numeric id."
        return None
    return value


async def validate_deployment(
    db: AsyncSession,
    allowed: AllowedPaths,
    project: Project,
    form: DeploymentForm,
    *,
    ignore_id: int | None = None,
    mount_points: Iterable[Path] | None = None,
) -> CleanDeployment:
    """Check every submitted value and return the clean ones, or raise with all the problems."""
    errors: dict[str, str] = {}
    name = form.name.strip()
    if not name:
        errors["name"] = "Enter a deployment name."
    elif len(name) > MAX_PROJECT_NAME_LENGTH or PROJECT_NAME_PATTERN.fullmatch(name) is None:
        errors["name"] = (
            "Use 1 to 100 lowercase letters, numbers and dashes, starting with a letter or number."
        )
    else:
        taken = select(Deployment.id).where(
            Deployment.project_id == project.id, Deployment.name == name
        )
        if ignore_id is not None:
            taken = taken.where(Deployment.id != ignore_id)
        if (await db.execute(taken)).first() is not None:
            errors["name"] = f"A deployment named {name} already exists in this project."

    root_check = check_root_path(allowed, form.root, mount_points=mount_points)
    if not root_check.ok:
        errors["root"] = root_check.message or "Enter a root path."

    dist_missing = _enum_value(DistMissingPolicy, form.dist_missing_policy)
    drift = _enum_value(DriftPolicy, form.drift_policy)
    missing_target = _enum_value(MissingTargetPolicy, form.missing_target_policy)
    policies = (
        (dist_missing, drift, missing_target) if dist_missing and drift and missing_target else None
    )
    if policies is None:
        errors["policies"] = "Choose one option for every policy."

    mode = form.file_mode.strip()
    if FILE_MODE_PATTERN.fullmatch(mode) is None:
        errors["file_mode"] = "Use an octal mode such as 0600."
    else:
        mode = mode.zfill(4)
    owner = _account(form.file_owner, "Owner", errors, "file_owner")
    group = _account(form.file_group, "Group", errors, "file_group")

    real_path = root_check.real_path
    if errors or real_path is None or policies is None:
        raise DeploymentFormError(errors, root_check if form.root.strip() else None)
    return CleanDeployment(
        name=name,
        root_path=str(real_path),
        dist_missing_policy=policies[0],
        drift_policy=policies[1],
        missing_target_policy=policies[2],
        file_mode=mode,
        file_owner=owner,
        file_group=group,
        root_check=root_check,
    )


def _policy_details(clean: CleanDeployment) -> dict[str, object]:
    """The audited values of a deployment: paths, policies and file defaults, no secrets."""
    return {
        "root_path": clean.root_path,
        "dist_missing_policy": clean.dist_missing_policy.value,
        "drift_policy": clean.drift_policy.value,
        "missing_target_policy": clean.missing_target_policy.value,
        "file_mode": clean.file_mode,
        "file_owner": clean.file_owner,
        "file_group": clean.file_group,
    }


async def create_deployment(
    db: AsyncSession,
    project: Project,
    clean: CleanDeployment,
    *,
    actor: User,
    ip_address: str | None = None,
) -> Deployment:
    """Create a deployment from validated values and audit it."""
    deployment = Deployment(
        project_id=project.id,
        name=clean.name,
        root_path=clean.root_path,
        dist_missing_policy=clean.dist_missing_policy,
        drift_policy=clean.drift_policy,
        missing_target_policy=clean.missing_target_policy,
        file_mode=clean.file_mode,
        file_owner=clean.file_owner,
        file_group=clean.file_group,
    )
    db.add(deployment)
    await db.flush()
    await record_audit(
        db,
        action="deployment.create",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=actor,
        ip_address=ip_address,
        target_type="deployment",
        target_id=deployment.id,
        target_name=deployment.name,
        project_id=project.id,
        deployment_id=deployment.id,
        details=_policy_details(clean),
    )
    await db.commit()
    return deployment


async def update_deployment(
    db: AsyncSession,
    deployment: Deployment,
    clean: CleanDeployment,
    *,
    actor: User,
    ip_address: str | None = None,
) -> Deployment:
    """Apply validated values to a deployment and audit what changed."""
    new_values = {
        "name": clean.name,
        "root_path": clean.root_path,
        "dist_missing_policy": clean.dist_missing_policy,
        "drift_policy": clean.drift_policy,
        "missing_target_policy": clean.missing_target_policy,
        "file_mode": clean.file_mode,
        "file_owner": clean.file_owner,
        "file_group": clean.file_group,
    }
    changed = [key for key, value in new_values.items() if getattr(deployment, key) != value]
    details: dict[str, object] = {"changed": changed}
    if "name" in changed:
        details["previous_name"] = deployment.name
    if "root_path" in changed:
        details["previous_root_path"] = deployment.root_path
    for key in changed:
        setattr(deployment, key, new_values[key])
    await record_audit(
        db,
        action="deployment.update",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=actor,
        ip_address=ip_address,
        target_type="deployment",
        target_id=deployment.id,
        target_name=deployment.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        details=details,
    )
    await db.commit()
    return deployment
