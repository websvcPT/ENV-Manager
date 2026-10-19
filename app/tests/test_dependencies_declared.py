"""Dependencies that were removed stay removed: nothing imports them and the project does not
declare them."""

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REMOVED = ("slowapi",)


def declared_names() -> set[str]:
    """The names of the runtime and development dependencies of pyproject.toml."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entries = [*data["project"]["dependencies"], *data["dependency-groups"]["dev"]]
    return {re.split(r"[\[=<>~! ]", entry, maxsplit=1)[0].lower() for entry in entries}


@pytest.mark.parametrize("name", REMOVED)
def test_a_removed_dependency_is_not_declared(name: str) -> None:
    assert name not in declared_names()


@pytest.mark.parametrize("name", REMOVED)
def test_a_removed_dependency_is_not_imported(name: str) -> None:
    pattern = re.compile(rf"^\s*(?:import|from)\s+{name}\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if path.name != Path(__file__).name and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    assert importlib.util.find_spec(name) is None
