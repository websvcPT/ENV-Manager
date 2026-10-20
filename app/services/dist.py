"""Dist comparison: read the dist template from the target and compare keys with an environment.

The report lists keys in the dist file missing from the environment, keys in the environment that
the dist file does not know, and keys with an empty value in the environment. It holds key names
and line numbers only, never values. Files that are not env format (certificates, free text) are
skipped.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.services.confined_read import ConfinedReadError, PathProblem, read_confined_text
from app.services.env_parser import ParsedEnv, parse_env
from app.services.paths import AllowedPaths

MAX_DIST_BYTES = 1024 * 1024


class DistProblem(StrEnum):
    """Why a dist comparison cannot run at all."""

    ENVIRONMENT_MISSING = "environment_missing"
    NO_VERSION = "no_version"
    ROOT_OUTSIDE = "root_outside"
    NOT_RELATIVE = "not_relative"
    LEAVES_ROOT = "leaves_root"
    NOT_A_FILE = "not_a_file"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    NOT_UTF8 = "not_utf8"


class DistError(Exception):
    """The dist comparison cannot run; ``problem`` says why and the message is safe to show.

    The message never names the deployment root or any other host path, because the caller may
    hold access to a single environment only.
    """

    def __init__(self, problem: DistProblem, message: str) -> None:
        """Hold the reason and its message."""
        super().__init__(message)
        self.problem = problem


class DistSkipReason(StrEnum):
    """Why key comparison was skipped."""

    ENVIRONMENT_NOT_ENV_FORMAT = "environment_not_env_format"
    DIST_NOT_ENV_FORMAT = "dist_not_env_format"
    NO_DIST_LINKED = "no_dist_linked"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class DistReport:
    """Key level result of comparing an environment with its dist file.

    ``missing_lines`` holds the line of the dist file where each missing key is defined, in the
    order of ``missing``.
    """

    skipped: DistSkipReason | None
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    empty: tuple[str, ...]
    missing_lines: tuple[int, ...] = ()

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
    missing = tuple(key for key in dist.keys if key not in environment.variables)
    return DistReport(
        skipped=None,
        missing=missing,
        extra=tuple(key for key in environment.keys if key not in dist.variables),
        empty=environment.empty_keys,
        missing_lines=tuple(dist.variables[key].line for key in missing),
    )


def compare_text_to_dist(environment_text: str, dist_text: str) -> DistReport:
    """Parse both texts and compare them."""
    return compare_to_dist(parse_env(environment_text), parse_env(dist_text))


_FIXED_MESSAGES = {
    PathProblem.ROOT_OUTSIDE: "deployment is outside the allowed paths",
    PathProblem.LEAVES_ROOT: "dist path leaves the deployment root",
}


def read_dist_file(allowed: AllowedPaths, deployment_root: str, dist_path: str) -> str:
    """Read the dist file at `dist_path` below the deployment root, as text.

    The deployment root and the dist file are resolved to real paths now and the opened file is
    checked again, so neither a symlink nor `..` can lead the read outside the deployment root
    and the allowed paths. Raises `DistError` for every failure, with a message free of paths.
    """
    try:
        return read_confined_text(
            allowed, deployment_root, dist_path, label="dist", max_bytes=MAX_DIST_BYTES
        )
    except ConfinedReadError as error:
        raise DistError(
            DistProblem(error.problem.value), _FIXED_MESSAGES.get(error.problem, str(error))
        ) from error
