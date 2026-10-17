"""Parse env files into variables and detect text that is not env format.

Parsing uses the python-dotenv tokenizer with interpolation off, so values are exactly what the
file says and never read the process environment. Compose has its own parser that differs from
python-dotenv in a few places (see the tests for the documented cases). The parsed result is
used for key comparison and emptiness checks only; a file is never rewritten from it.

Error reports carry line numbers only, never file content, because values can be secrets.
"""

import io
import re
from dataclasses import dataclass

from dotenv.parser import parse_stream

_VALID_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class EnvVariable:
    """One variable. ``assigned`` is false for a bare ``KEY`` line without ``=``."""

    key: str
    value: str
    assigned: bool
    line: int

    @property
    def empty(self) -> bool:
        """Whether the variable has no value, assigned empty or not assigned at all."""
        return self.value == ""


@dataclass(frozen=True)
class ParsedEnv:
    """Result of parsing a file.

    ``variables`` holds one entry per key in first-seen order; when a key repeats, the last
    assignment supplies the value and the key is listed in ``duplicate_keys``.
    ``problem_lines`` are the 1-based lines the tokenizer could not read or whose key is not a
    valid variable name. ``is_env_format`` is false for certificates and other free text.
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
        """Keys whose value is empty, in first-seen order."""
        return tuple(key for key, variable in self.variables.items() if variable.empty)


def parse_env(text: str) -> ParsedEnv:
    """Parse ``text`` as an env file.

    A file is env format when it has no unreadable line, every key is a valid variable name and,
    if it has any variable at all, at least one is assigned with ``=``. Empty and comment-only
    files are env format with no variables. A lone word or a PEM block is not.
    """
    variables: dict[str, EnvVariable] = {}
    duplicates: list[str] = []
    problems: list[int] = []
    for binding in parse_stream(io.StringIO(text)):
        line = binding.original.line
        if binding.error or (binding.key is not None and not _VALID_KEY.fullmatch(binding.key)):
            problems.append(line)
            continue
        if binding.key is None:
            continue
        variable = EnvVariable(
            key=binding.key,
            value=binding.value or "",
            assigned=binding.value is not None,
            line=line,
        )
        if binding.key in variables and binding.key not in duplicates:
            duplicates.append(binding.key)
        # Re-assigning keeps the first-seen position but takes the last value.
        variables[binding.key] = variable
    is_env_format = not problems and (
        not variables or any(variable.assigned for variable in variables.values())
    )
    return ParsedEnv(
        variables=variables,
        duplicate_keys=tuple(duplicates),
        problem_lines=tuple(problems),
        is_env_format=is_env_format,
    )
