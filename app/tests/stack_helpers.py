"""Helpers for tests that need Docker projects and Compose stacks on disk: the answers of a fake
``docker`` for the two reads stack discovery makes, and folders holding Compose files."""

import json
from pathlib import Path

from app.tests.fake_docker import FakeDocker

BEHAVIOR = """case "$*" in
  "compose ls"*) cat "$dir/ls.out"; cat "$dir/ls.err" >&2; exit "$(cat "$dir/ls.code")" ;;
  "ps --all"*) cat "$dir/labels.out"; exit "$(cat "$dir/labels.code")" ;;
esac
"""


def answer(
    docker: FakeDocker,
    projects: list[object] | str,
    labels: dict[str, str] | None = None,
    *,
    ls_code: int = 0,
    ls_error: str = "",
    labels_code: int = 0,
) -> None:
    """Make the fake print a ``compose ls`` document and the label lines, or fail."""
    text = projects if isinstance(projects, str) else json.dumps(projects)
    lines = "".join(
        json.dumps({"project": name, "folder": folder}) + "\n"
        for name, folder in (labels or {}).items()
    )
    files = {
        "ls.out": text,
        "ls.err": ls_error,
        "ls.code": str(ls_code),
        "labels.out": lines,
        "labels.code": str(labels_code),
    }
    for name, content in files.items():
        (docker.folder / name).write_text(content, encoding="utf-8")
    docker.behavior(BEHAVIOR)


def project(name: str, files: list[Path], status: str = "running(2)") -> dict[str, str]:
    """One entry of ``docker compose ls``."""
    return {"Name": name, "Status": status, "ConfigFiles": ",".join(str(path) for path in files)}


def make_stack(folder: Path, *names: str) -> Path:
    """A folder holding the named Compose files, the base one when none is named."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in names or ("compose.yaml",):
        (folder / name).write_text("services: {}\n", encoding="utf-8")
    return folder
