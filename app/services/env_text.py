"""Change variables of an env text in place.

The text is read with the same tokenizer as the parser. Every statement keeps its exact source
(comments, blank lines, ordering, ``export`` prefixes), and only the statements of variables
whose value changes are rewritten, so saving a value that did not change never touches the file.
The result is parsed again and must give back exactly the requested values, otherwise nothing is
returned.
"""

import io
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass

from dotenv.parser import parse_stream

from app.services.env_parser import parse_env

VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_./:@%+,=-]*")
_LEADING_BLANK_LINES = re.compile(r"(?:[ \t]*\n)*")
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

    Plain values are written bare. Anything else is single quoted, which both python-dotenv and
    Compose read literally (no escapes, no interpolation). A value holding a single quote or a
    backslash cannot be single quoted, so it is double quoted with those two escaped.
    """
    if _PLAIN_VALUE.fullmatch(value):
        return f"{key}={value}\n"
    if "'" not in value and "\\" not in value:
        return f"{key}='{value}'\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


def apply_changes(text: str, changes: VariableChanges) -> str:
    """The text with ``changes`` applied, everything else byte for byte as it was.

    A changed variable is rewritten where it is last assigned (the effective assignment) and
    keeps its ``export`` prefix; a removed variable loses every assignment; new variables are
    appended. Raises ``EnvEditError`` when the result would not read back as requested.
    """
    bindings = list(parse_stream(io.StringIO(text)))
    effective = parse_env(text).variables
    last_index = {
        binding.key: index for index, binding in enumerate(bindings) if binding.key is not None
    }
    pieces: list[str] = []
    for index, binding in enumerate(bindings):
        source = binding.original.string
        key = binding.key
        if key is None or binding.error:
            pieces.append(source)
            continue
        lead = _LEADING_BLANK_LINES.match(source)
        blank = lead.group(0) if lead else ""
        statement = source[len(blank) :]
        if key in changes.remove:
            pieces.append(blank)
        elif (
            key in changes.values
            and index == last_index[key]
            and (not effective[key].assigned or effective[key].value != changes.values[key])
        ):
            prefix = _EXPORT_PREFIX.match(statement)
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
