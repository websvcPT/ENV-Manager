"""A webhook address stored as plain text is sealed on upgrade, and nothing keeps the plain text.

The old database holds the address as an ordinary JSON setting under the name it keeps as a
secret. The step seals it in place, key rotation then moves it like any other secret, and a scan of
every table finds no trace of the address afterwards.
"""

import json
from pathlib import Path

from sqlalchemy import select

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.models import AppSetting, AuditEntry
from app.services.app_settings import WEBHOOK_URL_KEY, read_secret
from app.services.audit import AuditActor
from app.services.key_rotation import rotate_master_key
from app.services.webhook_url_upgrade import seal_plain_webhook_url
from app.tests.deploy_helpers import Harness
from app.tests.key_helpers import text_values

PLAIN_URL = "https://chat.example.com/hooks/T0001/B0002/token-sentinel-8c41"


async def store_plain(h: Harness, value: object = PLAIN_URL) -> None:
    """Put the address where an older version kept it: JSON text under the setting's name."""
    h.db.add(AppSetting(key=WEBHOOK_URL_KEY, value=json.dumps(value)))
    await h.db.commit()


def database_path(h: Harness) -> Path:
    """The file of the harness database."""
    return Path(str(h.database.url.database))


def cells_holding(path: Path, text: str) -> list[tuple[str, str]]:
    """The table and column of every cell that holds the text."""
    return [(table, column) for table, column, value in text_values(path) if text in value]


async def test_a_plain_address_is_sealed_in_place_and_no_table_keeps_it(harness: Harness) -> None:
    h = harness
    await store_plain(h)
    assert cells_holding(database_path(h), "token-sentinel-8c41") == [("app_settings", "value")]

    assert await seal_plain_webhook_url(h.db, h.crypto) is True

    assert await read_secret(h.db, h.crypto, WEBHOOK_URL_KEY) == PLAIN_URL
    assert cells_holding(database_path(h), "token-sentinel-8c41") == []
    assert cells_holding(database_path(h), "chat.example.com") == []
    row = await h.db.get(AppSetting, WEBHOOK_URL_KEY)
    assert row is not None and row.value.startswith("v1$")


async def test_the_audit_entry_of_the_move_names_the_setting_and_nothing_else(
    harness: Harness,
) -> None:
    h = harness
    await store_plain(h)
    await seal_plain_webhook_url(h.db, h.crypto)

    query = select(AuditEntry).where(AuditEntry.action == "settings.update")
    entries = (await h.db.execute(query)).scalars()
    text = json.dumps([[entry.details, entry.target_name, entry.actor_name] for entry in entries])
    assert WEBHOOK_URL_KEY in text and "upgrade" in text
    assert "chat.example.com" not in text and "token-sentinel" not in text


async def test_running_the_step_again_leaves_a_sealed_address_alone(harness: Harness) -> None:
    h = harness
    await store_plain(h)
    assert await seal_plain_webhook_url(h.db, h.crypto) is True
    row = await h.db.get(AppSetting, WEBHOOK_URL_KEY)
    assert row is not None
    sealed = row.value

    assert await seal_plain_webhook_url(h.db, h.crypto) is False

    assert row.value == sealed
    assert await read_secret(h.db, h.crypto, WEBHOOK_URL_KEY) == PLAIN_URL


async def test_a_database_without_an_address_is_left_alone(harness: Harness) -> None:
    assert await seal_plain_webhook_url(harness.db, harness.crypto) is False
    assert await harness.db.get(AppSetting, WEBHOOK_URL_KEY) is None


async def test_an_empty_plain_address_is_dropped(harness: Harness) -> None:
    h = harness
    await store_plain(h, "")
    assert await seal_plain_webhook_url(h.db, h.crypto) is True
    assert await h.db.get(AppSetting, WEBHOOK_URL_KEY) is None


async def test_the_sealed_address_follows_a_key_rotation(harness: Harness) -> None:
    h = harness
    await store_plain(h)
    await seal_plain_webhook_url(h.db, h.crypto)
    new = CryptoService(parse_master_key(generate_master_key()))

    await rotate_master_key(h.db, old=h.crypto, new=new, actor=AuditActor.cli(), backup=None)

    assert await read_secret(h.db, new, WEBHOOK_URL_KEY) == PLAIN_URL
    assert cells_holding(database_path(h), "token-sentinel-8c41") == []
