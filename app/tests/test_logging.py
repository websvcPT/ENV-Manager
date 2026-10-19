"""Tests for the application's logging setup."""

import logging
from collections.abc import Iterator

import pytest

from app.core.log import configure_logging
from app.services.webhook import HttpxWebhookTransport
from app.tests.webhook_helpers import Receiver

NOISY = ("httpx", "httpcore", "aiosqlite")


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Put the handlers and levels back after a test that configures logging."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in NOISY}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, previous in levels.items():
        logging.getLogger(name).setLevel(previous)


@pytest.mark.usefixtures("restored_logging")
def test_libraries_that_log_addresses_or_parameters_stay_quiet_even_at_debug() -> None:
    """The HTTP client puts the whole address, which can hold a token, into its log lines, and
    the SQLite driver puts statement parameters into its; neither may follow the log level."""
    configure_logging("debug")
    for name in NOISY:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


@pytest.mark.usefixtures("restored_logging")
async def test_a_webhook_post_logs_nothing_that_holds_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even at debug level the address (path and token included) never reaches the log."""
    configure_logging("debug")
    logging.getLogger().addHandler(caplog.handler)
    async with Receiver() as receiver:
        await HttpxWebhookTransport().post(receiver.url, {}, b"{}")
    assert "url-token-91c3" not in caplog.text and "/hook" not in caplog.text
