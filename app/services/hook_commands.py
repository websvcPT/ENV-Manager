"""Post-deploy command lines: parsing and the safety net.

A command is one line, split into words with ``shlex`` and never given to a shell, so pipes,
redirects and ``&&`` do not exist: an unquoted operator is refused instead of being passed on as
an argument. The first word must be exactly ``docker``. Words are passed on exactly as parsed:
nothing in a command is substituted, so a Docker ``--format '{{.Names}}'`` template or any text
in braces reaches Docker as written.

The safety net refuses what could destroy a deployment's containers or data: more than
``MAX_COMMANDS`` commands, a line longer than ``MAX_LINE_LENGTH`` characters, ``compose down``,
any ``prune`` and the ``-v`` and ``--volumes`` options of ``rm``. The rules read the parsed words
and err on the side of refusing: a word ``down`` after ``compose``, a word ``prune`` anywhere and
a volumes option anywhere after a word ``rm`` are refused even when a name happens to look like
one. Saving and running use the same check. Error messages name a line number and the rule,
never the text of the line.
"""

import re
import shlex

DOCKER_WORD = "docker"
MAX_COMMANDS = 10
MAX_LINE_LENGTH = 500

_OPERATOR_CHARACTERS = frozenset("();<>|&")
_LINE_BREAK = re.compile(r"\r\n|\n|\r")
# A short option cluster with a v ("-v", "-fv", "-sfv", "-v=true"), as pflag reads it.
_SHORT_VOLUMES = re.compile(r"-[A-Za-z]*v[A-Za-z]*(=.*)?")

Command = tuple[str, ...]


class CommandError(ValueError):
    """A command line is refused; the message is safe to show."""


def _has_control_character(text: str) -> bool:
    """Whether the text holds a control character other than a tab."""
    return any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in text)


def _is_volumes_option(word: str) -> bool:
    """Whether a word is the ``--volumes`` option or a short option cluster that holds ``-v``."""
    return (
        word == "--volumes"
        or word.startswith("--volumes=")
        or _SHORT_VOLUMES.fullmatch(word) is not None
    )


def _check_safety(words: Command, number: int) -> None:
    """Refuse a command that removes a project, prunes Docker data or removes volumes.

    ``words`` start with ``docker``. Raises ``CommandError`` naming the line.
    """
    rest = words[1:]
    if "prune" in rest:
        raise CommandError(f"Line {number}: prune commands are not allowed.")
    if "compose" in rest and "down" in rest[rest.index("compose") + 1 :]:
        raise CommandError(
            f"Line {number}: docker compose down is not allowed. "
            "Use docker compose rm -sf and docker compose up -d to restart one service."
        )
    if "rm" in rest and any(_is_volumes_option(word) for word in rest[rest.index("rm") + 1 :]):
        raise CommandError(
            f"Line {number}: the -v and --volumes options of rm are not allowed, "
            "because they delete volumes."
        )


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
    _check_safety(words, number)
    return words


def parse_commands(text: str) -> tuple[Command, ...]:
    """The commands of the stored text, one per non-blank line, each split into words.

    Raises ``CommandError`` for a line whose first word is not ``docker``, that is too long,
    holds an unquoted shell operator or an unclosed quote, or breaks a safety rule, and for more
    than ``MAX_COMMANDS`` commands.
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
