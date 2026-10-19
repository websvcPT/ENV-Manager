"""Where git sync keeps its files inside the data volume.

One private folder (mode 0700) holds the working clone, a home folder for the git and ssh
children (so they never read a real user's configuration) and a scratch folder where the
credential files of a single call live for as long as that call runs.
"""

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services.git_errors import GitProblem, GitSyncError

FOLDER_MODE = 0o700


@dataclass(frozen=True)
class GitLocations:
    """The folders of git sync below one root."""

    root: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> GitLocations:
        """The git folder inside the data volume of the startup configuration."""
        return cls(settings.git_path)

    @property
    def work(self) -> Path:
        """The working clone the export is written into and pushed from."""
        return self.root / "work"

    @property
    def home(self) -> Path:
        """The home folder of git and ssh children."""
        return self.root / "home"

    @property
    def scratch(self) -> Path:
        """The folder of per-call temporary files."""
        return self.root / "tmp"

    def ensure(self) -> None:
        """Create the folders with mode 0700, or tighten existing ones to it.

        A symlink or a file in the place of a folder is refused, so a swapped folder cannot
        redirect the export or the credential files.
        """
        try:
            for folder in (self.root, self.home, self.scratch):
                folder.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
                info = folder.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise GitSyncError(GitProblem.WORKDIR)
                if stat.S_IMODE(info.st_mode) != FOLDER_MODE:
                    folder.chmod(FOLDER_MODE)
        except OSError as error:
            raise GitSyncError(GitProblem.WORKDIR) from error

    def reset_work(self) -> None:
        """Remove the working clone, so the next push starts from a fresh one."""
        try:
            if self.work.is_symlink():
                os.unlink(self.work)
            else:
                shutil.rmtree(self.work, ignore_errors=True)
        except OSError as error:
            raise GitSyncError(GitProblem.WORKDIR) from error
