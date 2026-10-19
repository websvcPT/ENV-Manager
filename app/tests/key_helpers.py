"""Helpers shared by the key rotation tests: whole-database dumps and a scan for sealed values."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

CIPHERTEXT = re.compile(r"^v\d+\$[A-Za-z0-9_=-]+$")
HASH = re.compile(r"^[0-9a-f]{64}$")


def dump_database(path: Path, *, without_audit: bool = False) -> list[str]:
    """Every statement that rebuilds the database: equal dumps mean equal databases.

    A refused operation writes its audit entry, so a check that nothing else changed leaves the
    audit rows out with ``without_audit``.
    """
    with closing(sqlite3.connect(path)) as connection:
        statements = list(connection.iterdump())
    if without_audit:
        return [line for line in statements if not line.startswith('INSERT INTO "audit_entries"')]
    return statements


def text_values(path: Path) -> list[tuple[str, str, str]]:
    """The table, column and value of every text cell of every table."""
    found: list[tuple[str, str, str]] = []
    with closing(sqlite3.connect(path)) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for table in tables:
            cursor = connection.execute(f'SELECT * FROM "{table}"')  # noqa: S608
            names = [column[0] for column in cursor.description]
            for row in cursor:
                found.extend(
                    (table, name, value)
                    for name, value in zip(names, row, strict=True)
                    if isinstance(value, str)
                )
    return found


def sealed_values(path: Path) -> list[tuple[str, str, str]]:
    """The cells that look like ciphertext of this application (a version tag and a body)."""
    return [cell for cell in text_values(path) if CIPHERTEXT.match(cell[2])]


def hash_values(path: Path) -> list[tuple[str, str, str]]:
    """The cells that look like a keyed hash (64 hex digits), whatever the column."""
    return [cell for cell in text_values(path) if HASH.match(cell[2])]
