"""The rule of display names, which project and deployment names follow.

A display name has letters in any case, numbers, spaces and ``. - _``, at most 100 characters
once trimmed and with repeated spaces made one, and at least one letter or number, so a name of
dots and dashes is refused. The letters are ASCII only: SQLite folds the case of ASCII letters
alone, so the unique indexes on ``lower(name)`` and the checks in code agree on what a duplicate
is. A slash can never appear, which keeps ``project/deployment/service`` paths of the command
line unambiguous.
"""

import re

MAX_DISPLAY_NAME_LENGTH = 100
DISPLAY_NAME_PATTERN = re.compile(r"[A-Za-z0-9 ._-]+")
REPEATED_SPACES = re.compile(r" {2,}")


def normalize_display_name(raw: str) -> str:
    """The name as it is stored: trimmed, with every run of spaces made one."""
    return REPEATED_SPACES.sub(" ", raw.strip())


def is_display_name(name: str, min_length: int) -> bool:
    """Whether a normalized name follows the rule with at least ``min_length`` characters."""
    return (
        min_length <= len(name) <= MAX_DISPLAY_NAME_LENGTH
        and DISPLAY_NAME_PATTERN.fullmatch(name) is not None
        and any(character.isalnum() for character in name)
    )


def display_name_rule(min_length: int) -> str:
    """The rule in words, for the hint under a name field."""
    return (
        f"{min_length} to {MAX_DISPLAY_NAME_LENGTH} characters with at least one letter or "
        "number. Letters, numbers, spaces and the characters . - _ are allowed."
    )


def display_name_message(min_length: int) -> str:
    """The refusal of a name that breaks the rule."""
    return (
        f"Use {min_length} to {MAX_DISPLAY_NAME_LENGTH} characters with at least one letter or "
        "number: letters, numbers, spaces and the characters . - _"
    )
