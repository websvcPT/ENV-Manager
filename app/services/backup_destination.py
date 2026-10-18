"""The extra destination of backups: a folder inside the allowed paths.

The folder is checked when it is saved and again every time a backup is copied there, with the
allowed paths loaded fresh by the caller. The copy is made through the same descriptor-confined
write as deploy targets: the folder is opened without following a symlink, its real location is
checked against the allowed paths, and the file is created exclusively inside it, so a link
swapped in after the check cannot lead the copy outside the allowed paths. Nothing is ever
deleted there, and no message names a path.
"""

import os
from pathlib import Path

from app.services.backup import FILE_MODE
from app.services.backup_files import BackupFileMissingError, open_backup_file
from app.services.paths import (
    AllowedPaths,
    PathNotAllowedError,
    PathValidationError,
    normalize_path,
)
from app.services.target_files import TargetError, TargetLocation, TargetProblem, write_target_file

OUTSIDE_MESSAGE = "Must be inside an allowed path."
DESTINATION_MESSAGES = {
    TargetProblem.ROOT_OUTSIDE: "The extra destination is outside the allowed paths.",
    TargetProblem.LEAVES_ROOT: "The extra destination is outside the allowed paths.",
    TargetProblem.FOLDER_MISSING: "The extra destination folder does not exist.",
    TargetProblem.NOT_A_FOLDER: "The extra destination is not a plain folder.",
}
COPY_FAILED_MESSAGE = "The copy to the extra destination failed."


class ExtraDestinationError(Exception):
    """The extra destination cannot be used; the message is safe to show and to store."""


def check_extra_destination(allowed: AllowedPaths, raw: str) -> Path:
    """The real folder of a typed extra destination, or ``ExtraDestinationError``.

    The path must be absolute, resolve inside the allowed paths, exist as a folder and be one the
    application can write to. The real path (symlinks followed) is what callers store.
    """
    try:
        path = normalize_path(raw)
        real = allowed.resolve(path)
    except PathNotAllowedError as error:
        raise ExtraDestinationError(OUTSIDE_MESSAGE) from error
    except PathValidationError as error:
        raise ExtraDestinationError(str(error).capitalize() + ".") from error
    if not real.exists():
        raise ExtraDestinationError("The folder does not exist.")
    if not real.is_dir():
        raise ExtraDestinationError("That is not a folder.")
    if not os.access(real, os.W_OK | os.X_OK):
        raise ExtraDestinationError("The application cannot write to this folder.")
    return real


def copy_to_extra_destination(
    backup_dir: Path, name: str, destination: str, allowed: AllowedPaths
) -> None:
    """Copy one backup file from the backup folder into the extra destination, mode 0600.

    ``allowed`` must be freshly loaded by the caller. The folder is resolved now; a destination
    that is no longer inside the allowed paths is refused. Raises ``ExtraDestinationError`` with
    a fixed message.
    """
    try:
        real = allowed.resolve(destination)
    except PathValidationError as error:
        raise ExtraDestinationError(DESTINATION_MESSAGES[TargetProblem.ROOT_OUTSIDE]) from error
    location = TargetLocation(root=real, directory=real, name=name, relative=name)
    try:
        opened = open_backup_file(backup_dir, name)
    except BackupFileMissingError as error:
        raise ExtraDestinationError(COPY_FAILED_MESSAGE) from error
    try:
        with os.fdopen(opened.descriptor, "rb") as source:
            write_target_file(location, allowed, source, mode=FILE_MODE)
    except TargetError as error:
        message = DESTINATION_MESSAGES.get(error.problem, COPY_FAILED_MESSAGE)
        raise ExtraDestinationError(message) from error
    except OSError as error:
        raise ExtraDestinationError(COPY_FAILED_MESSAGE) from error
