"""Change variables of an env text in place.

The text is read with the same statements as the parser. Every statement keeps its exact source
(comments, blank lines, ordering, ``export`` prefixes), and only the statements of variables
whose value changes are rewritten, so saving a value that did not change never touches the file.
The result is parsed again and must give back exactly the requested values, otherwise nothing is
returned.
"""

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass

from app.services.env_parser import parse_env, parse_statements

VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_./:@%+,=-]*")
_LEADING_BLANK_LINES = re.compile(r"(?:[ \t\r]*\n)*")
_EXPORT_PREFIX = re.compile(r"[ \t]*export[ \t]+")


class EnvEditError(Exception):
    """The edit cannot be written back so that the file reads as requested."""


@dataclass(frozen=True)
class VariableChanges:
    """Values to set (existing or new keys) and keys to remove."""

    values: Mapping[str, str]
    remove: Set[str]


def format_assignment(key: str, value: str) -> str:
    """One ``KEY=value`` statement with a line break.

    Plain values are written bare. Anything else is single quoted, which Compose reads literally
    (no escapes, no interpolation). A value holding a single quote or a backslash cannot be
    single quoted, so it is double quoted with the backslash, the double quote and the dollar
    sign escaped (a bare dollar sign would start an interpolation).
    """
    if _PLAIN_VALUE.fullmatch(value):
        return f"{key}={value}\n"
    if "'" not in value and "\\" not in value:
        return f"{key}='{value}'\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'{key}="{escaped}"\n'


def apply_changes(text: str, changes: VariableChanges) -> str:
    """The text with ``changes`` applied, everything else byte for byte as it was.

    A changed variable is rewritten where it is last assigned (the effective assignment) and
    keeps its ``export`` prefix; a removed variable loses every assignment; new variables are
    appended. Raises ``EnvEditError`` when the result would not read back as requested.
    """
    statements = parse_statements(text)
    effective = parse_env(text).variables
    last_index = {
        statement.key: index
        for index, statement in enumerate(statements)
        if statement.key is not None
    }
    pieces: list[str] = []
    for index, statement in enumerate(statements):
        source = statement.source
        key = statement.key
        if key is None or statement.error:
            pieces.append(source)
            continue
        lead = _LEADING_BLANK_LINES.match(source)
        blank = lead.group(0) if lead else ""
        after_blank = source[len(blank) :]
        if key in changes.remove:
            pieces.append(blank)
        elif (
            key in changes.values
            and index == last_index[key]
            and (not effective[key].assigned or effective[key].value != changes.values[key])
        ):
            prefix = _EXPORT_PREFIX.match(after_blank)
            pieces.append(
                blank
                + (prefix.group(0) if prefix else "")
                + format_assignment(key, changes.values[key])
            )
        else:
            pieces.append(source)
    result = "".join(pieces)
    for key, value in changes.values.items():
        if key not in effective:
            if result and not result.endswith("\n"):
                result += "\n"
            result += format_assignment(key, value)
    parsed = parse_env(result).variables
    for key, value in changes.values.items():
        if key in changes.remove or key not in parsed or parsed[key].value != value:
            raise EnvEditError("A value cannot be written so that it reads back unchanged.")
    if any(key in parsed for key in changes.remove):
        raise EnvEditError("A variable could not be removed.")
    return result
