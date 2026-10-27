"""The name rule of a Compose project.

Docker Compose accepts lowercase letters, numbers, dashes and underscores, starting with a
letter or a number. The limit of 63 characters is the one of a DNS label, which the network and
container names Compose derives from the project name must fit. A deployment's project name is
never derived from its display name: it is the name its containers already carry.
"""

import re

MAX_COMPOSE_PROJECT_LENGTH = 63
COMPOSE_PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
COMPOSE_PROJECT_MESSAGE = (
    f"Use 1 to {MAX_COMPOSE_PROJECT_LENGTH} lowercase letters, numbers, dashes and underscores, "
    "starting with a letter or number."
)


def is_compose_project_name(name: str) -> bool:
    """Whether the (already trimmed) name follows the Compose project name rule."""
    return (
        len(name) <= MAX_COMPOSE_PROJECT_LENGTH
        and COMPOSE_PROJECT_PATTERN.fullmatch(name) is not None
    )
