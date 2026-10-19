"""Scanning an export for anything readable: the check every git sync test shares.

An export may hold ciphertext and hashed paths and nothing else. The scan collects what must not
appear (every seeded value and every item name), then looks at everything a clone of the
repository shows: the paths, the files, the commit authors and messages and every object of the
repository, decompressed. Ciphertext is base64 text, in which a short name turns up by chance, so
a ciphertext file is also opened up and searched as bytes, and the short needles are searched
there only.
"""

import base64
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import Deployment, Environment, EnvironmentVersion, Project, Service, User
from app.seeds import EDITOR_SECRET_VALUES
from app.services.env_parser import parse_env
from app.tests.git_helpers import git, tool

HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_FILE = re.compile(r"^\d{6}\.enc$")
FIXED_FILES = {"manifest.enc", "snapshot.enc"}
CIPHERTEXT = re.compile(r"^v\d+\$[A-Za-z0-9_=-]+$")
LONG_NEEDLE = 8
MIN_BINARY_NEEDLE = 5


@dataclass(frozen=True)
class Needles:
    """What an export must not contain: secret text and item names."""

    values: frozenset[str]
    names: frozenset[str]

    @property
    def all(self) -> frozenset[str]:
        """Every needle."""
        return self.values | self.names


async def collect_needles(engine: AsyncEngine, crypto: CryptoService) -> Needles:
    """The plaintext of every stored version and every name in the database."""
    values: set[str] = set(EDITOR_SECRET_VALUES)
    names: set[str] = set()
    async with create_session_factory(engine)() as db:
        for version in (await db.execute(select(EnvironmentVersion))).scalars():
            text = crypto.decrypt_text(version.content_encrypted)
            values.add(text.strip())
            for line in text.splitlines():
                if len(line.strip()) >= LONG_NEEDLE:
                    values.add(line.strip())
            for variable in parse_env(text).variables.values():
                if len(variable.value) >= LONG_NEEDLE:
                    values.add(variable.value)
        for model in (Project, Deployment, Service, Environment, User):
            for name in (await db.execute(select(model.name))).scalars():
                names.add(name)
        for root, target, dist in (
            await db.execute(
                select(Deployment.root_path, Environment.target_path, Environment.dist_path)
                .join(Service, Service.deployment_id == Deployment.id)
                .join(Environment, Environment.service_id == Service.id)
            )
        ).all():
            names.update(item for item in (root, target, dist) if item)
    return Needles(frozenset(v for v in values if v), frozenset(n for n in names if n))


def check_paths(clone: Path) -> list[str]:
    """Every path in the clone, after checking that each part is a hash or a fixed name."""
    listed = git("ls-tree", "-r", "--name-only", "HEAD", cwd=clone).splitlines()
    for path in listed:
        parts = path.split("/")
        if len(parts) == 1:
            assert parts[0] in FIXED_FILES, path
        else:
            assert parts[0] == "versions", path
            assert HASH_PATTERN.fullmatch(parts[1]), path
            assert parts[2] == "meta.enc" or VERSION_FILE.fullmatch(parts[2]), path
    return listed


def _forms(content: bytes) -> list[tuple[bytes, int]]:
    """The ways to read a ciphertext file with the shortest needle each can be searched for.

    As base64 text a short needle turns up by chance, so only long ones are searched there; the
    bytes inside are random, so a needle of five bytes is already safe to search for.
    """
    body = content.decode("ascii").strip().split("$", 1)[1]
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    return [(content, LONG_NEEDLE), (raw, MIN_BINARY_NEEDLE)]


def scan_tree(clone: Path, needles: Needles) -> list[str]:
    """Every needle found in the files of a clone, for the test to assert it is empty.

    Every file must be a ciphertext token, which is also what makes a short name safe to skip.
    """
    found: list[str] = []
    for path in clone.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        content = path.read_bytes()
        assert CIPHERTEXT.fullmatch(content.decode("ascii").strip()), f"{path} is not ciphertext"
        for data, shortest in _forms(content):
            for needle in needles.all:
                if len(needle) >= shortest and needle.encode() in data:
                    found.append(f"{needle!r} in {path.name}")
    return found


def scan_history(clone: Path, needles: Needles) -> list[str]:
    """Every long needle in any object of the repository, and any needle in commit text."""
    found: list[str] = []
    people = git("log", "--format=%an%n%ae%n%cn%n%ce%n%B", cwd=clone)
    for needle in needles.all:
        if needle in people:
            found.append(f"{needle!r} in a commit")
    objects = tool("git", "cat-file", "--batch-all-objects", "--batch", cwd=clone)
    for needle in needles.all:
        if len(needle) >= LONG_NEEDLE and needle.encode() in objects:
            found.append(f"{needle!r} in an object")
    return found
