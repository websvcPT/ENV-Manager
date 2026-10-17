"""Application logging. All modules log through this module, never ``print``."""

import logging
import sys

LOGGER_NAME = "envmanager"
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with a stderr handler at the given level name."""
    logging.basicConfig(level=level.upper(), format=_FORMAT, stream=sys.stderr, force=True)
    # The SQLite driver logs statement parameters at debug level, which would expose values.
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the application namespace."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
