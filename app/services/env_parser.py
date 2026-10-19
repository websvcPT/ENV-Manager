"""Parse env files into variables and detect text that is not env format.

The reader follows the rules Docker Compose applies to an ``env_file`` (checked against the real
``docker compose config`` output by the Compose fixtures of the tests and the real scenarios):

- a line is ``KEY=value`` with optional spaces around the ``=`` and an optional ``export``
  prefix; a line with a key only is a bare ``KEY`` that Compose takes from the host environment.
  Compose also reads ``KEY: value``; that form is not accepted here, so a YAML or other text
  file is never taken for an env file;
- a key is made of letters, digits and ``_ . - [ ]``, so ``a.b``, ``1A`` and ``A[1]`` are keys;
- an unquoted value ends at a space followed by ``#`` and is trimmed;
- a single quoted value is literal, except that ``\\'`` stands for a quote, and nothing after
  the closing quote counts;
- a double quoted value decodes ``\\a \\b \\f \\n \\r \\t \\v \\" \\\\ \\$ \\uXXXX`` and keeps every
  other backslash as written;
- ``$$`` is a dollar sign, and ``$NAME``, ``${NAME}`` and the ``:-``, ``-``, ``:?``, ``?``, ``:+``
  and ``+`` forms of ``${...}`` interpolate in unquoted and double quoted values.

The host environment is never read. A value that interpolates keeps the template as written and
is flagged, because what Compose will make of it is not known here; a bare key has no value at
all. Neither is ever reported as empty. The parsed result is used for key comparison, emptiness
checks and value comparison only; a file is never rewritten from it.

Error reports carry line numbers only, never file content, because values can be secrets.
"""

import re
from dataclasses import dataclass

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BRACE_BODY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?::?[-?+].*)?", re.DOTALL)
_EXPORT_PREFIX = re.compile(r"export[ \t]+(?=\S)")
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")
_KEY_PUNCTUATION = "_.-[]"
_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    '"': '"',
    "\\": "\\",
    "$": "$$",
}


@dataclass(frozen=True)
class EnvVariable:
    """One variable.

    ``assigned`` is false for a bare ``KEY`` line. ``interpolated`` is true when the value holds
    a ``$NAME`` or ``${...}`` reference, kept as written because Compose resolves it from the
    host environment.
    """

    key: str
    value: str
    assigned: bool
    line: int
    interpolated: bool = False

    @property
    def empty(self) -> bool:
        """Whether the value is known to be empty (never a bare key or an interpolated value)."""
        return self.assigned and not self.interpolated and self.value == ""


@dataclass(frozen=True)
class ParsedEnv:
    """Result of parsing a file.

    ``variables`` holds one entry per key in first-seen order; when a key repeats, the last
    assignment supplies the value and the key is listed in ``duplicate_keys``.
    ``problem_lines`` are the 1-based lines Compose would refuse. ``is_env_format`` is false for
    certificates and other free text.
    """

    variables: dict[str, EnvVariable]
    duplicate_keys: tuple[str, ...]
    problem_lines: tuple[int, ...]
    is_env_format: bool

    @property
    def keys(self) -> tuple[str, ...]:
        """Keys in first-seen order."""
        return tuple(self.variables)

    @property
    def empty_keys(self) -> tuple[str, ...]:
        """Keys whose value is known to be empty, in first-seen order."""
        return tuple(key for key, variable in self.variables.items() if variable.empty)


@dataclass(frozen=True)
class Statement:
    """One statement of a file with its exact source.

    ``source`` holds the blank lines before the statement and its own lines with their line
    breaks, so the sources of all statements join back into the file. ``line`` is the line the
    statement itself starts on. A comment or trailing blank text has no key; an unreadable
    statement has no key and ``error`` set; ``value`` is None for a bare key.
    """

    source: str
    line: int
    key: str | None
    value: str | None
    interpolated: bool
    error: bool


def _line_end(text: str, position: int) -> int:
    """The index after the line break of the line starting at ``position`` (or the text end)."""
    index = text.find("\n", position)
    return len(text) if index < 0 else index + 1


def _unescape(text: str) -> str:
    """Decode the escapes of a double quoted value; ``\\$`` becomes ``$$`` for the dollar pass."""
    pieces: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if char != "\\" or not following:
            pieces.append(char)
            index += 1
        elif following in _ESCAPES:
            pieces.append(_ESCAPES[following])
            index += 2
        elif following == "0":
            # Compose drops the zero of a "\0" and keeps the backslash.
            pieces.append("\\")
            index += 2
        elif following == "u" and _HEX4.fullmatch(text[index + 2 : index + 6]):
            code = int(text[index + 2 : index + 6], 16)
            pieces.append(text[index : index + 6] if 0xD800 <= code <= 0xDFFF else chr(code))
            index += 6
        else:
            pieces.append(char)
            index += 1
    return "".join(pieces)


def _expand_dollars(text: str) -> tuple[str, bool] | None:
    """Turn ``$$`` into ``$`` and report whether a reference is present.

    References stay as written. Returns None for a template Compose refuses (a ``${`` that is
    not closed or whose body is not a name with an optional operator).
    """
    pieces: list[str] = []
    interpolated = False
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if char != "$":
            pieces.append(char)
            index += 1
        elif following == "$":
            pieces.append("$")
            index += 2
        elif following == "{":
            depth = 0
            end = -1
            for position in range(index + 1, len(text)):
                if text[position] == "{":
                    depth += 1
                elif text[position] == "}":
                    depth -= 1
                    if depth == 0:
                        end = position
                        break
            if end < 0 or not _BRACE_BODY.fullmatch(text[index + 2 : end]):
                return None
            pieces.append(text[index : end + 1])
            interpolated = True
            index = end + 1
        else:
            name = _NAME.match(text, index + 1)
            if name is None:
                pieces.append("$")
                index += 1
            else:
                pieces.append(text[index : name.end()])
                interpolated = True
                index = name.end()
    return "".join(pieces), interpolated


def _scan_single_quoted(text: str, start: int) -> tuple[str, int] | None:
    """The literal value after an opening single quote and the index after the closing one."""
    pieces: list[str] = []
    index = start
    while index < len(text):
        pair = text[index : index + 2]
        if pair == "\\'":
            pieces.append("'")
            index += 2
        elif pair == "\\\\":
            pieces.append(pair)
            index += 2
        elif text[index] == "'":
            return "".join(pieces), index + 1
        else:
            pieces.append(text[index])
            index += 1
    return None


def _scan_double_quoted(text: str, start: int) -> tuple[str, int] | None:
    """The raw content after an opening double quote and the index after the closing one."""
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return text[start:index], index + 1
        else:
            index += 1
    return None


def _valid_key(key: str) -> bool:
    """Whether Compose accepts the text as a variable name."""
    return bool(key) and all(char.isalnum() or char in _KEY_PUNCTUATION for char in key)


def _read_statement(text: str, position: int) -> tuple[str | None, str | None, bool, int] | None:
    """Read the statement whose line starts at ``position``.

    Returns the key, the value (None for a bare key), the interpolated flag and the index after
    the statement, or None when the statement cannot be read.
    """
    line_end = _line_end(text, position)
    content_end = line_end - 1 if text[line_end - 1 : line_end] == "\n" else line_end
    line_text = text[position:content_end]
    start = position + len(line_text) - len(line_text.lstrip(" \t﻿"))
    prefix = _EXPORT_PREFIX.match(text, start, content_end)
    if prefix:
        start = prefix.end()
    body = text[start:content_end]
    separator = body.find("=")
    key = (body if separator < 0 else body[:separator]).strip()
    if not _valid_key(key):
        return None
    if separator < 0:
        return key, None, False, line_end
    value_start = start + separator + 1
    while value_start < content_end and text[value_start] in " \t":
        value_start += 1
    opening = text[value_start : value_start + 1]
    if opening == "'":
        scanned = _scan_single_quoted(text, value_start + 1)
        if scanned is None:
            return None
        return key, scanned[0], False, _line_end(text, scanned[1])
    if opening == '"':
        double = _scan_double_quoted(text, value_start + 1)
        if double is None:
            return None
        expanded = _expand_dollars(_unescape(double[0]))
        if expanded is None:
            return None
        return key, expanded[0], expanded[1], _line_end(text, double[1])
    segment = text[value_start:content_end]
    comment = segment.find(" #")
    if comment >= 0:
        segment = segment[:comment]
    expanded = _expand_dollars(segment.rstrip())
    if expanded is None:
        return None
    return key, expanded[0], expanded[1], line_end


def parse_statements(text: str) -> list[Statement]:
    """Split ``text`` into statements whose sources join back into the text."""
    statements: list[Statement] = []
    position = 0
    line = 1
    while position < len(text):
        cursor = position
        blank_lines = 0
        while cursor < len(text) and not text[cursor : _line_end(text, cursor)].strip():
            cursor = _line_end(text, cursor)
            blank_lines += 1
        statement_line = line + blank_lines
        if cursor >= len(text):
            statements.append(Statement(text[position:], statement_line, None, None, False, False))
            break
        line_end = _line_end(text, cursor)
        if text[cursor:line_end].lstrip().startswith("#"):
            statements.append(
                Statement(text[position:line_end], statement_line, None, None, False, False)
            )
            end = line_end
        else:
            read = _read_statement(text, cursor)
            if read is None:
                end = line_end
                statements.append(
                    Statement(text[position:end], statement_line, None, None, False, True)
                )
            else:
                key, value, interpolated, end = read
                statements.append(
                    Statement(text[position:end], statement_line, key, value, interpolated, False)
                )
        line += text.count("\n", position, end)
        position = end
    return statements


def parse_env(text: str) -> ParsedEnv:
    """Parse ``text`` as an env file.

    A file is env format when it has no unreadable line and, if it has any variable at all, at
    least one is assigned with ``=``. Empty and comment-only files are env format with
    no variables. A lone word or a PEM block is not.
    """
    variables: dict[str, EnvVariable] = {}
    duplicates: list[str] = []
    problems: list[int] = []
    for statement in parse_statements(text):
        if statement.error:
            problems.append(statement.line)
            continue
        if statement.key is None:
            continue
        variable = EnvVariable(
            key=statement.key,
            value=statement.value or "",
            assigned=statement.value is not None,
            line=statement.line,
            interpolated=statement.interpolated,
        )
        if statement.key in variables and statement.key not in duplicates:
            duplicates.append(statement.key)
        # Re-assigning keeps the first-seen position but takes the last value.
        variables[statement.key] = variable
    is_env_format = not problems and (
        not variables or any(variable.assigned for variable in variables.values())
    )
    return ParsedEnv(
        variables=variables,
        duplicate_keys=tuple(duplicates),
        problem_lines=tuple(problems),
        is_env_format=is_env_format,
    )
