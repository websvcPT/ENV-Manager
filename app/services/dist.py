"""Dist comparison: read the dist template from the target and compare keys with an environment.

The report lists keys in the dist file missing from the environment, keys in the environment that
the dist file does not know, and keys with an empty value in the environment. It holds key names
only, never values. Files that are not env format (certificates, free text) are skipped.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import Deployment, Environment, EnvironmentVersion, Service
from app.services.env_parser import ParsedEnv, parse_env
from app.services.paths import AllowedPaths, PathValidationError, is_within, resolve_real_path

MAX_DIST_BYTES = 1024 * 1024


class DistError(Exception):
    """The dist comparison cannot run; the message is safe to show to the user."""


class DistSkipReason(StrEnum):
    """Why key comparison was skipped."""

    ENVIRONMENT_NOT_ENV_FORMAT = "environment_not_env_format"
    DIST_NOT_ENV_FORMAT = "dist_not_env_format"


@dataclass(frozen=True)
class DistReport:
    """Key level result of comparing an environment with its dist file."""

    skipped: DistSkipReason | None
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    empty: tuple[str, ...]

    @property
    def clean(self) -> bool:
        """Whether nothing was found; a skipped comparison is not clean."""
        return self.skipped is None and not (self.missing or self.extra or self.empty)


def compare_to_dist(environment: ParsedEnv, dist: ParsedEnv) -> DistReport:
    """Compare parsed contents. Missing follows dist order, extra and empty follow file order."""
    if not environment.is_env_format:
        return DistReport(DistSkipReason.ENVIRONMENT_NOT_ENV_FORMAT, (), (), ())
    if not dist.is_env_format:
        return DistReport(DistSkipReason.DIST_NOT_ENV_FORMAT, (), (), ())
    return DistReport(
        skipped=None,
        missing=tuple(key for key in dist.keys if key not in environment.variables),
        extra=tuple(key for key in environment.keys if key not in dist.variables),
        empty=environment.empty_keys,
    )


def compare_text_to_dist(environment_text: str, dist_text: str) -> DistReport:
    """Parse both texts and compare them."""
    return compare_to_dist(parse_env(environment_text), parse_env(dist_text))


def read_dist_file(allowed: AllowedPaths, deployment_root: str, dist_path: str) -> str:
    """Read the dist file at ``dist_path`` below the deployment root, as text.

    The deployment root and the dist file are resolved to real paths now; the file must lie
    inside the real deployment root and inside the allowed paths, so neither a symlink nor
    ``..`` can lead the read elsewhere. Raises ``DistError`` for every failure.
    """
    relative = PurePosixPath(dist_path.strip())
    if not dist_path.strip() or relative.is_absolute() or ".." in relative.parts:
        raise DistError("dist path must be a relative path inside the deployment")
    try:
        root = allowed.resolve(deployment_root)
        real_path = allowed.resolve(root / relative)
    except PathValidationError as error:
        raise DistError(str(error)) from error
    if not is_within(real_path, resolve_real_path(root)):
        raise DistError("dist path leaves the deployment root")
    return _read_text(real_path)


def _read_text(real_path: Path) -> str:
    """Read a regular UTF-8 file of bounded size."""
    if not real_path.is_file():
        raise DistError("dist file does not exist or is not a regular file")
    try:
        with real_path.open("rb") as handle:
            data = handle.read(MAX_DIST_BYTES + 1)
    except OSError as error:
        raise DistError("dist file cannot be read") from error
    if len(data) > MAX_DIST_BYTES:
        raise DistError("dist file is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DistError("dist file is not valid UTF-8 text") from error


async def check_environment_dist(
    db: AsyncSession,
    crypto: CryptoService,
    allowed: AllowedPaths,
    environment_id: int,
) -> DistReport:
    """Compare the latest saved version of an environment with its dist file in the target.

    ``allowed`` must be freshly loaded by the caller. Raises ``DistError`` when the environment
    has no dist link, no saved version, or the dist file cannot be read.
    """
    row = (
        await db.execute(
            select(Environment, Deployment.root_path)
            .join(Service, Service.id == Environment.service_id)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .where(Environment.id == environment_id)
        )
    ).one_or_none()
    if row is None:
        raise DistError("environment does not exist")
    environment, root_path = row
    if not environment.dist_path:
        raise DistError("environment has no dist file linked")
    latest = (
        await db.execute(
            select(EnvironmentVersion)
            .where(EnvironmentVersion.environment_id == environment_id)
            .order_by(EnvironmentVersion.number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise DistError("environment has no saved version")
    dist_text = read_dist_file(allowed, root_path, environment.dist_path)
    return compare_text_to_dist(crypto.decrypt_text(latest.content_encrypted), dist_text)
