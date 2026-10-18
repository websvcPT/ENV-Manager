"""Reading the details document of an audit entry for display and export.

Entries must never hold secret values, so nothing here is meant to find one. It is a second
line of defence for data that should not exist: values under keys that name secret material are
replaced, every text is bounded, and the result is plain text that the templates escape.
"""

import json
from dataclasses import dataclass

HIDDEN_TEXT = "hidden"
MAX_TEXT_LENGTH = 4000
MAX_DEPTH = 6
CODE_MIN_LENGTH = 160

# Keys that would hold secret material if someone stored it. Key names of environment variables
# (the `keys` of a reveal) are names, not values, and are not in this list.
SECRET_KEYS = frozenset(
    {
        "value",
        "values",
        "password",
        "new_password",
        "old_password",
        "passphrase",
        "secret",
        "secrets",
        "token_secret",
        "token_value",
        "api_key",
        "private_key",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "content",
        "plaintext",
    }
)


@dataclass(frozen=True)
class DetailLine:
    """One key of the details, as text. ``code`` marks long or multi-line text such as output."""

    key: str
    text: str
    code: bool


def _is_secret_key(key: object) -> bool:
    """Whether a details key names secret material."""
    return str(key).strip().lower().replace("-", "_").replace(" ", "_") in SECRET_KEYS


def redact(value: object, depth: int = 0) -> object:
    """A copy of the details with the values of secret-looking keys replaced and depth bounded."""
    if depth >= MAX_DEPTH:
        return HIDDEN_TEXT
    if isinstance(value, dict):
        return {
            str(key): HIDDEN_TEXT if _is_secret_key(key) else redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, depth + 1) for item in value]
    return value


def _bounded(text: str) -> str:
    """The text cut to a display limit, with a marker when something was left out."""
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[:MAX_TEXT_LENGTH] + "..."


def _scalar(value: object) -> str:
    """A scalar as text: booleans as yes and no, a missing value as None."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _flat(value: object) -> str:
    """A value on one line: lists joined with commas, mappings as ``key value`` pairs."""
    if isinstance(value, dict):
        pieces = [f"{key} {_flat(item)}" for key, item in value.items() if item not in (None, "")]
        return " · ".join(pieces)
    if isinstance(value, list):
        return ", ".join(_flat(item) for item in value)
    return _scalar(value)


def _text_of(value: object) -> str:
    """A value as display text: a list of mappings gets one line per mapping."""
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        return "\n".join(_flat(item) for item in value)
    return _flat(value)


def detail_lines(details: object) -> tuple[DetailLine, ...]:
    """The details as one line per key, redacted and bounded. Empty values are left out."""
    safe = redact(details)
    if not isinstance(safe, dict):
        safe = {"details": safe} if safe not in (None, "", [], {}) else {}
    lines: list[DetailLine] = []
    for key, value in safe.items():
        if value in (None, "", [], {}):
            continue
        text = _bounded(_text_of(value))
        code = isinstance(value, str) and ("\n" in value or len(value) > CODE_MIN_LENGTH)
        lines.append(DetailLine(key.replace("_", " "), text, code))
    return tuple(lines)


def details_json(details: object) -> str:
    """The redacted details as compact JSON for the export, empty when there are none."""
    safe = redact(details)
    if safe in (None, "", [], {}):
        return ""
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str)
