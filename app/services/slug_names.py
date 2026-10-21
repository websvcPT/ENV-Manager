"""The name rule of services.

A service usually mirrors a Compose service, so its name keeps a strict slug: lowercase letters,
numbers and dashes, starting with a letter or a number, 1 to 100 characters. Project and
deployment names are display names with their own looser rule (``app.services.display_names``).
"""

import re

MAX_SLUG_LENGTH = 100
SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
SLUG_MESSAGE = (
    f"Use 1 to {MAX_SLUG_LENGTH} lowercase letters, numbers and dashes, "
    "starting with a letter or number."
)


def is_slug_name(name: str) -> bool:
    """Whether the (already trimmed) name follows the slug rule."""
    return len(name) <= MAX_SLUG_LENGTH and SLUG_PATTERN.fullmatch(name) is not None
