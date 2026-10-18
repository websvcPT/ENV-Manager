"""Post-deploy command lines: parsing, validation and placeholder substitution.

A command is one line, split into words with ``shlex`` and never given to a shell, so pipes,
redirects and ``&&`` do not exist: an unquoted operator is refused instead of being passed on as
an argument. The first word must be exactly ``docker``. The placeholders ``{deployment_path}``,
``{deployment_name}`` and ``{service_name}`` are replaced inside the words after the line was
split, in one pass and with validated values, so a value can never add or split an argument and
text that merely looks like a placeholder (a Docker ``--format '{{.Names}}'`` template, a typo)
stays as written. Error messages name a line number, never the text of the line.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

DOCKER_WORD = "docker"
MAX_COMMANDS = 20
MAX_LINE_LENGTH = 1000
MAX_VALUE_LENGTH = 4096
PLACEHOLDERS = ("deployment_path", "deployment_name", "service_name")

_OPERATOR_CHARACTERS = frozenset("();<>|&")
_PLACEHOLDER = re.compile(r"\{(deployment_path|deployment_name|service_name)\}")
_NAME_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_LINE_BREAK = re.compile(r"\r\n|\n|\r")

Command = tuple[str, ...]


class CommandError(ValueError):
    """A command line or a placeholder value is refused; the message is safe to show."""


@dataclass(frozen=True)
class PlaceholderValues:
    """The validated values of the three placeholders."""

    deployment_path: str
    deployment_name: str
    service_name: str

    @classmethod
    def validated(
        cls, deployment_path: str, deployment_name: str, service_name: str
    ) -> PlaceholderValues:
        """The values when each is acceptable as part of an argument, else ``CommandError``.

        The path must be absolute and free of control characters; the names must be plain names
        that cannot start an option.
        """
        path = PurePosixPath(deployment_path)
        if (
            not path.is_absolute()
            or len(deployment_path) > MAX_VALUE_LENGTH
            or _has_control_character(deployment_path)
        ):
            raise CommandError("The deployment path cannot be used in a command.")
        for name in (deployment_name, service_name):
            if len(name) > MAX_VALUE_LENGTH or _NAME_VALUE.fullmatch(name) is None:
                raise CommandError("A deployment or service name cannot be used in a command.")
        return cls(deployment_path, deployment_name, service_name)

    def lookup(self, placeholder: str) -> str:
        """The value of a placeholder name."""
        return str(getattr(self, placeholder))


def _has_control_character(text: str) -> bool:
    """Whether the text holds a control character other than a tab."""
    return any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in text)


def _split_line(line: str, number: int) -> Command:
    """The words of one command line, or ``CommandError`` naming the line."""
    if len(line) > MAX_LINE_LENGTH:
        raise CommandError(f"Line {number} is longer than {MAX_LINE_LENGTH} characters.")
    if _has_control_character(line):
        raise CommandError(f"Line {number} contains a control character.")
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        words = tuple(lexer)
    except ValueError as error:
        raise CommandError(f"Line {number}: a quote is not closed.") from error
    if not words or words[0] != DOCKER_WORD:
        raise CommandError(f"Line {number}: the first word must be {DOCKER_WORD}.")
    if len(words) == 1:
        raise CommandError(f"Line {number}: say what {DOCKER_WORD} should do.")
    if any(word and set(word) <= _OPERATOR_CHARACTERS for word in words):
        raise CommandError(
            f"Line {number}: shell operators are not supported. "
            f"Use one plain {DOCKER_WORD} command per line, without pipes, redirects or &&."
        )
    return words


def parse_commands(text: str) -> tuple[Command, ...]:
    """The commands of the stored text, one per non-blank line, each split into words.

    Raises ``CommandError`` for a line whose first word is not ``docker``, that holds an unquoted
    shell operator or an unclosed quote, and for more than ``MAX_COMMANDS`` commands.
    """
    commands: list[Command] = []
    for number, raw in enumerate(_LINE_BREAK.split(text), start=1):
        line = raw.strip()
        if not line:
            continue
        if len(commands) == MAX_COMMANDS:
            raise CommandError(f"Use at most {MAX_COMMANDS} commands.")
        commands.append(_split_line(line, number))
    return tuple(commands)


def normalize_commands(text: str) -> str:
    """The stored form of the commands: stripped lines without blanks, one per line."""
    lines = (line.strip() for line in _LINE_BREAK.split(text))
    return "\n".join(line for line in lines if line)


def substitute(command: Command, values: PlaceholderValues) -> Command:
    """The command with the placeholders replaced inside each word.

    Replacement happens once per word, so a value is never scanned for placeholders again, and
    the number of words never changes.
    """
    return tuple(
        _PLACEHOLDER.sub(lambda match: values.lookup(match.group(1)), word) for word in command
    )
