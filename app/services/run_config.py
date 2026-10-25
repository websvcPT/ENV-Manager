"""The private Docker config folder of one command run.

The ``docker`` command reads the registry logins it sends from the ``config.json`` of its config
folder (``DOCKER_CONFIG``). The app keeps no such file between runs: ``private_docker_config``
makes a new folder inside the scratch directory of the Docker CLI for one run, writes the logins
into it and removes the whole folder when the run is over, however it ended (success, failure,
timeout or cancellation). The folder is readable by the app's user only (mode 0700) and the file
is created with mode 0600, so it is never readable by anyone else, not even for a moment.
"""

import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.services.registries import RegistryLogin, docker_config
from app.services.scratch_sweep import RUN_FOLDER_PREFIX

CONFIG_FILE_NAME = "config.json"
CONFIG_FILE_MODE = 0o600


class RunConfigError(Exception):
    """The folder or its file could not be written. The message never holds a path or a value."""


@contextmanager
def private_docker_config(scratch: Path, logins: Iterable[RegistryLogin]) -> Iterator[Path]:
    """A new private folder below ``scratch`` that holds the logins as a Docker ``config.json``.

    The folder is removed when the block ends. Raises ``RunConfigError`` when it cannot be made
    or written (nothing is left behind then).
    """
    folder: Path | None = None
    try:
        try:
            folder = Path(tempfile.mkdtemp(prefix=RUN_FOLDER_PREFIX, dir=scratch))
            descriptor = os.open(
                folder / CONFIG_FILE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, CONFIG_FILE_MODE
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(docker_config(logins), handle)
        except OSError as error:
            raise RunConfigError("The Docker config of the run could not be written.") from error
        yield folder
    finally:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
