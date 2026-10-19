"""The release version is written twice and must name one release."""

import tomllib
from pathlib import Path

from app import VERSION

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def project_version(pyproject: Path) -> str:
    """The version a pyproject.toml declares under its ``[project]`` table."""
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert isinstance(declared, str)
    return declared


def test_the_version_constant_equals_the_version_of_the_project_file() -> None:
    """``app.VERSION`` (release, error reporting, diagnostics) and pyproject.toml move together."""
    assert project_version(PYPROJECT) == VERSION


def test_the_reader_takes_the_version_of_the_project_table(tmp_path: Path) -> None:
    """Another ``version`` key in the file does not stand in for the project's own."""
    other = tmp_path / "pyproject.toml"
    other.write_text(
        '[tool.other]\nversion = "9.9.9"\n\n[project]\nname = "x"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    assert project_version(other) == "1.2.3"
