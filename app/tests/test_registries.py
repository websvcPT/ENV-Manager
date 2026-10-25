"""Docker registry logins: host rules, add, update, remove, sealing, audit and the Docker config.

The tests run on the seeded temporary database and read rows, audit entries and the database file
back, so a token that reached any of them in plain text is found.
"""

import base64
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.models import AuditEntry, DockerRegistry
from app.seeds import SeedData
from app.services import registries
from app.services.registries import (
    ACTION_ADD,
    ACTION_REMOVE,
    ACTION_UPDATE,
    DOCKER_HUB_KEY,
    HOST_EMPTY_MESSAGE,
    HOST_INVALID_MESSAGE,
    HOST_PATH_MESSAGE,
    HOST_PORT_MESSAGE,
    TOKEN_CHARACTERS_MESSAGE,
    TOKEN_LENGTH_MESSAGE,
    TOKEN_REQUIRED_MESSAGE,
    USERNAME_CHARACTERS_MESSAGE,
    USERNAME_COLON_MESSAGE,
    USERNAME_EMPTY_MESSAGE,
    USERNAME_LENGTH_MESSAGE,
    RegistryFormError,
    RegistryLogin,
    RegistryNotFoundError,
    RegistryTokenError,
    add_registry,
    docker_config,
    list_registries,
    load_registry_logins,
    normalize_host,
    registry_context,
    remove_registry,
    update_registry,
)
from app.tests.deploy_helpers import audit_of
from app.tests.key_helpers import text_values

TOKEN = "dckr_pat_Zq81-secret.token_value"
OTHER_TOKEN = "dckr_pat_replacement-token-9f3"
ZERO_WIDTH_SPACE = chr(0x200B)
LINE_SEPARATOR = chr(0x2028)
TOKEN_REFUSED = {"token": TOKEN_CHARACTERS_MESSAGE}


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("registry.example.com", "registry.example.com"),
        ("  Registry.Example.COM  ", "registry.example.com"),
        ("https://registry.example.com", "registry.example.com"),
        ("HTTP://registry.example.com/", "registry.example.com"),
        ("registry.example.com/", "registry.example.com"),
        ("localhost:5000", "localhost:5000"),
        ("127.0.0.1:5000", "127.0.0.1:5000"),
        ("https://Registry.Example.com:8443/", "registry.example.com:8443"),
        ("registry.example.com:65535", "registry.example.com:65535"),
        ("registry.example.com:1", "registry.example.com:1"),
        ("my_registry.internal", "my_registry.internal"),
        ("docker.io", "docker.io"),
        ("a", "a"),
        ("ghcr.io", "ghcr.io"),
    ],
)
def test_a_typed_host_is_stored_lowercase_without_scheme_or_trailing_slash(
    typed: str, stored: str
) -> None:
    assert normalize_host(typed) == stored


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("", HOST_EMPTY_MESSAGE),
        ("   ", HOST_EMPTY_MESSAGE),
        ("https://", HOST_EMPTY_MESSAGE),
        ("/", HOST_EMPTY_MESSAGE),
        ("registry.example.com/v2", HOST_PATH_MESSAGE),
        ("registry.example.com//", HOST_PATH_MESSAGE),
        ("https://registry.example.com/team/image", HOST_PATH_MESSAGE),
        ("reg istry.example.com", HOST_INVALID_MESSAGE),
        ("registry.example.com\nother.example.com", HOST_INVALID_MESSAGE),
        ("user@registry.example.com", HOST_INVALID_MESSAGE),
        ("ftp://registry.example.com", HOST_INVALID_MESSAGE),
        ("registry.example.com?x=1", HOST_INVALID_MESSAGE),
        ("registry.example.com#top", HOST_INVALID_MESSAGE),
        ("[::1]:5000", HOST_INVALID_MESSAGE),
        ("-registry.example.com", HOST_INVALID_MESSAGE),
        ("registry-.example.com", HOST_INVALID_MESSAGE),
        ("registry..example.com", HOST_INVALID_MESSAGE),
        ("registry.example.com.", HOST_INVALID_MESSAGE),
        ("räksmorgås.se", HOST_INVALID_MESSAGE),
        (":5000", HOST_INVALID_MESSAGE),
        ("a" * 254, HOST_INVALID_MESSAGE),
        ("registry.example.com:", HOST_PORT_MESSAGE),
        ("registry.example.com:0", HOST_PORT_MESSAGE),
        ("registry.example.com:65536", HOST_PORT_MESSAGE),
        ("registry.example.com:05000", HOST_PORT_MESSAGE),
        ("registry.example.com:http", HOST_PORT_MESSAGE),
        ("registry.example.com:5000:5000", HOST_PORT_MESSAGE),
        ("registry.example.com:" + "9" * 5000, HOST_PORT_MESSAGE),
    ],
)
def test_a_host_that_is_not_a_host_name_with_an_optional_port_is_refused(
    typed: str, message: str
) -> None:
    with pytest.raises(RegistryFormError) as refused:
        normalize_host(typed)
    assert refused.value.errors == {"host": message}


async def add(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    host: str = "registry.example.com",
    username: str = "bot",
    token: str = TOKEN,
    ip_address: str | None = None,
) -> DockerRegistry:
    """Add a registry as the seeded superadmin."""
    return await add_registry(
        db, crypto, host, username, token, actor=seed_data.users.superadmin, ip_address=ip_address
    )


async def test_an_added_registry_is_stored_normalized_with_its_token_sealed_to_the_row(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    added = await add(
        db, crypto, seed_data, host="HTTPS://Registry.Example.com:5000/", username=" bot "
    )
    db.expire_all()
    (row,) = await list_registries(db)
    assert (row.id, row.host, row.username) == (added.id, "registry.example.com:5000", "bot")
    assert TOKEN not in row.token_encrypted
    assert crypto.decrypt_text(row.token_encrypted, registry_context(row.id)) == TOKEN
    assert registry_context(7) == b"registry:7"


async def test_the_token_is_nowhere_in_plain_text_not_in_the_database_and_not_in_the_audit_log(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    database_path: Path,
) -> None:
    actor = seed_data.users.superadmin
    registry = await add(db, crypto, seed_data)
    await update_registry(
        db, crypto, registry.id, "registry.example.com", "robot", OTHER_TOKEN, actor=actor
    )
    await update_registry(db, crypto, registry.id, "registry.example.org", "robot", "", actor=actor)
    await remove_registry(db, registry.id, actor=actor)
    await add(db, crypto, seed_data, host="ghcr.io")

    cells = [value for _, _, value in text_values(database_path)]
    for secret in (TOKEN, OTHER_TOKEN):
        assert not any(secret in cell for cell in cells)
    for file in (database_path, database_path.with_name(database_path.name + "-wal")):
        if file.exists():
            assert TOKEN.encode() not in file.read_bytes()
            assert OTHER_TOKEN.encode() not in file.read_bytes()
    query = select(AuditEntry).where(AuditEntry.target_type == "registry")
    entries = (await db.execute(query)).scalars()
    texts = [json.dumps(entry.details) + str(entry.target_name) for entry in entries]
    assert len(texts) == 5
    assert not any(secret in text for text in texts for secret in (TOKEN, OTHER_TOKEN))


async def test_adding_records_the_host_and_username_with_the_actor_and_address(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    registry = await add(db, crypto, seed_data, ip_address="203.0.113.9")
    (entry,) = await audit_of(db, ACTION_ADD)
    assert entry.action == "registry.add"
    assert entry.target_type == "registry"
    assert entry.target_id == registry.id
    assert entry.target_name == "registry.example.com"
    assert entry.details == {"host": "registry.example.com", "username": "bot"}
    assert entry.actor_name == seed_data.users.superadmin.name
    assert entry.actor_user_id == seed_data.users.superadmin.id
    assert entry.ip_address == "203.0.113.9"
    assert entry.project_id is None


@pytest.mark.parametrize(
    ("host", "username", "token", "errors"),
    [
        ("", "bot", TOKEN, {"host": HOST_EMPTY_MESSAGE}),
        ("a/b", "bot", TOKEN, {"host": HOST_PATH_MESSAGE}),
        ("registry.example.com", "", TOKEN, {"username": USERNAME_EMPTY_MESSAGE}),
        ("registry.example.com", "   ", TOKEN, {"username": USERNAME_EMPTY_MESSAGE}),
        (
            "registry.example.com",
            "a" * 256,
            TOKEN,
            {"username": USERNAME_LENGTH_MESSAGE},
        ),
        (
            "registry.example.com",
            "bo\nt",
            TOKEN,
            {"username": USERNAME_CHARACTERS_MESSAGE},
        ),
        (
            "registry.example.com",
            "bo\x00t",
            TOKEN,
            {"username": USERNAME_CHARACTERS_MESSAGE},
        ),
        ("registry.example.com", "bo:t", TOKEN, {"username": USERNAME_COLON_MESSAGE}),
        ("registry.example.com", "bot", "", {"token": TOKEN_REQUIRED_MESSAGE}),
        ("registry.example.com", "bot", "   ", {"token": TOKEN_REQUIRED_MESSAGE}),
        ("registry.example.com", "bot", "x" * 4097, {"token": TOKEN_LENGTH_MESSAGE}),
        ("registry.example.com", "bot", "tok\nen", TOKEN_REFUSED),
        ("registry.example.com", "bot", "token\n", TOKEN_REFUSED),
        ("registry.example.com", "bot", "tok\ten", TOKEN_REFUSED),
        ("registry.example.com", "bot", "tok\x00en", TOKEN_REFUSED),
        ("registry.example.com", "bot", f"tok{ZERO_WIDTH_SPACE}en", TOKEN_REFUSED),
        ("registry.example.com", "bot", f"tok{LINE_SEPARATOR}en", TOKEN_REFUSED),
        (
            "",
            "",
            "",
            {
                "host": HOST_EMPTY_MESSAGE,
                "username": USERNAME_EMPTY_MESSAGE,
                "token": TOKEN_REQUIRED_MESSAGE,
            },
        ),
    ],
)
async def test_refused_values_name_every_field_and_store_and_audit_nothing(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    host: str,
    username: str,
    token: str,
    errors: dict[str, str],
) -> None:
    with pytest.raises(RegistryFormError) as refused:
        await add(db, crypto, seed_data, host=host, username=username, token=token)
    assert refused.value.errors == errors
    assert await list_registries(db) == []
    assert await audit_of(db, ACTION_ADD) == []


async def test_a_long_username_and_token_at_the_limits_and_odd_characters_are_accepted(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    token = "p4ss word:with/odd=chars" + "é" * 10 + "x" * (4096 - 34)
    assert len(token) == 4096
    registry = await add(db, crypto, seed_data, username="é" * 255, token=token)
    assert len(registry.username) == 255
    (login,) = await load_registry_logins(db, crypto)
    assert login.token == token


async def test_a_host_is_unique_ignoring_case_and_scheme_and_the_refusal_names_it(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    await add(db, crypto, seed_data, host="registry.example.com")
    typed_variants = (
        "REGISTRY.Example.com",
        "https://registry.example.com/",
        "  registry.example.com",
    )
    for typed in typed_variants:
        with pytest.raises(RegistryFormError) as refused:
            await add(db, crypto, seed_data, host=typed)
        assert refused.value.errors == {
            "host": "The registry registry.example.com is already in the list."
        }
    await add(db, crypto, seed_data, host="registry.example.com:5000")
    assert len(await list_registries(db)) == 2
    assert len(await audit_of(db, ACTION_ADD)) == 2


@pytest.mark.parametrize("other", ["docker.io", "index.docker.io", "registry-1.docker.io"])
async def test_the_docker_hub_names_are_one_registry_and_share_one_login(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, other: str
) -> None:
    first = next(host for host in ("docker.io", "index.docker.io") if host != other)
    await add(db, crypto, seed_data, host=first)
    with pytest.raises(RegistryFormError) as refused:
        await add(db, crypto, seed_data, host=other)
    assert refused.value.errors == {
        "host": f"The registry {other} is the same as {first}, which is already in the list."
    }
    assert [row.host for row in await list_registries(db)] == [first]


async def test_a_duplicate_that_slips_past_the_check_is_refused_and_the_session_stays_usable(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add(db, crypto, seed_data, host="registry.example.com")

    async def nothing_is_taken(*args: object, **kwargs: object) -> None:
        """Stand for a second request that stored the host after the check ran."""

    monkeypatch.setattr(registries, "_require_free", nothing_is_taken)
    with pytest.raises(RegistryFormError) as refused:
        await add(db, crypto, seed_data, host="Registry.Example.com")
    assert refused.value.errors == {
        "host": "The registry registry.example.com is already in the list."
    }
    assert len(await list_registries(db)) == 1
    await add(db, crypto, seed_data, host="ghcr.io")
    assert len(await list_registries(db)) == 2


async def test_registries_are_listed_in_host_order(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    for host in ("quay.io", "ghcr.io", "docker.io", "registry.example.com"):
        await add(db, crypto, seed_data, host=host)
    assert [row.host for row in await list_registries(db)] == [
        "docker.io",
        "ghcr.io",
        "quay.io",
        "registry.example.com",
    ]


async def test_an_update_with_a_token_replaces_it_and_one_without_keeps_the_stored_one(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    actor = seed_data.users.superadmin
    registry = await add(db, crypto, seed_data)
    registry_id = registry.id
    sealed_before = registry.token_encrypted
    created = registry.created_at

    await update_registry(db, crypto, registry_id, "registry.example.com", "robot", "", actor=actor)
    db.expire_all()
    row = await db.get(DockerRegistry, registry_id)
    assert row is not None
    assert (row.username, row.token_encrypted) == ("robot", sealed_before)
    assert crypto.decrypt_text(row.token_encrypted, registry_context(row.id)) == TOKEN
    assert row.updated_at > created and row.created_at == created

    await update_registry(
        db, crypto, registry_id, "registry.example.com", "robot", "   ", actor=actor
    )
    db.expire_all()
    row = await db.get(DockerRegistry, registry_id)
    assert row is not None and row.token_encrypted == sealed_before

    await update_registry(
        db, crypto, registry_id, "Registry.Example.com:5000", "robot", OTHER_TOKEN, actor=actor
    )
    db.expire_all()
    row = await db.get(DockerRegistry, registry_id)
    assert row is not None
    assert row.host == "registry.example.com:5000"
    assert row.token_encrypted != sealed_before
    assert crypto.decrypt_text(row.token_encrypted, registry_context(row.id)) == OTHER_TOKEN
    (login,) = await load_registry_logins(db, crypto)
    assert login == RegistryLogin("registry.example.com:5000", "robot", OTHER_TOKEN)


async def test_an_update_audits_what_changed_and_never_the_token(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    actor = seed_data.users.superadmin
    registry = await add(db, crypto, seed_data)

    await update_registry(
        db, crypto, registry.id, "registry.example.com", "bot", OTHER_TOKEN, actor=actor
    )
    await update_registry(
        db, crypto, registry.id, "ghcr.io", "robot", "", actor=actor, ip_address="203.0.113.9"
    )
    first, second = await audit_of(db, ACTION_UPDATE)

    assert first.details == {
        "host": "registry.example.com",
        "username": "bot",
        "host_changed": False,
        "username_changed": False,
        "token_changed": True,
    }
    assert second.details == {
        "host": "ghcr.io",
        "username": "robot",
        "host_changed": True,
        "username_changed": True,
        "token_changed": False,
        "previous_host": "registry.example.com",
        "previous_username": "bot",
    }
    for entry in (first, second):
        assert entry.target_type == "registry" and entry.target_id == registry.id
        assert entry.actor_name == actor.name
        assert OTHER_TOKEN not in json.dumps(entry.details)
        assert TOKEN not in json.dumps(entry.details)
    assert first.target_name == "registry.example.com" and second.target_name == "ghcr.io"
    assert first.ip_address is None and second.ip_address == "203.0.113.9"


async def test_an_update_that_changes_nothing_writes_nothing(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    registry = await add(db, crypto, seed_data)
    stamp = registry.updated_at
    same = await update_registry(
        db,
        crypto,
        registry.id,
        "HTTPS://registry.example.com/",
        " bot ",
        "",
        actor=seed_data.users.superadmin,
    )
    assert same.updated_at == stamp
    assert await audit_of(db, ACTION_UPDATE) == []


async def test_an_update_cannot_take_the_host_of_another_registry_but_may_keep_its_own(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    actor = seed_data.users.superadmin
    first = await add(db, crypto, seed_data, host="registry.example.com")
    second = await add(db, crypto, seed_data, host="ghcr.io")
    with pytest.raises(RegistryFormError) as refused:
        await update_registry(db, crypto, second.id, "REGISTRY.example.com", "bot", "", actor=actor)
    assert refused.value.errors == {
        "host": "The registry registry.example.com is already in the list."
    }
    await update_registry(db, crypto, first.id, "Registry.Example.com", "robot", "", actor=actor)
    db.expire_all()
    assert [(row.host, row.username) for row in await list_registries(db)] == [
        ("ghcr.io", "bot"),
        ("registry.example.com", "robot"),
    ]
    assert len(await audit_of(db, ACTION_UPDATE)) == 1


async def test_refused_values_on_update_leave_the_registry_as_it_was(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    registry = await add(db, crypto, seed_data)
    registry_id = registry.id
    before = (registry.host, registry.username, registry.token_encrypted)
    with pytest.raises(RegistryFormError) as refused:
        await update_registry(
            db, crypto, registry_id, "a/b", "", "tok\nen", actor=seed_data.users.superadmin
        )
    assert refused.value.errors == {
        "host": HOST_PATH_MESSAGE,
        "username": USERNAME_EMPTY_MESSAGE,
        "token": TOKEN_CHARACTERS_MESSAGE,
    }
    db.expire_all()
    row = await db.get(DockerRegistry, registry_id)
    assert row is not None
    assert (row.host, row.username, row.token_encrypted) == before
    assert await audit_of(db, ACTION_UPDATE) == []


async def test_removing_deletes_the_row_and_audits_the_host_and_username(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    actor = seed_data.users.superadmin
    gone = await add(db, crypto, seed_data, host="registry.example.com")
    kept = await add(db, crypto, seed_data, host="ghcr.io")
    removed = await remove_registry(db, gone.id, actor=actor, ip_address="203.0.113.9")
    assert removed.host == "registry.example.com"
    assert [row.id for row in await list_registries(db)] == [kept.id]
    (entry,) = await audit_of(db, ACTION_REMOVE)
    assert entry.target_type == "registry" and entry.target_id == gone.id
    assert entry.target_name == "registry.example.com"
    assert entry.details == {"host": "registry.example.com", "username": "bot"}
    assert entry.ip_address == "203.0.113.9"
    with pytest.raises(RegistryNotFoundError) as missing:
        await remove_registry(db, gone.id, actor=actor)
    assert missing.value.errors == {"form": registries.NOT_FOUND_MESSAGE}
    assert len(await audit_of(db, ACTION_REMOVE)) == 1


async def test_a_registry_that_does_not_exist_cannot_be_updated(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    with pytest.raises(RegistryNotFoundError):
        await update_registry(
            db, crypto, 999, "ghcr.io", "bot", TOKEN, actor=seed_data.users.superadmin
        )
    assert await audit_of(db, ACTION_UPDATE) == []


async def test_a_removed_registry_id_is_never_given_to_a_later_registry(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    first = await add(db, crypto, seed_data, host="registry.example.com")
    await remove_registry(db, first.id, actor=seed_data.users.superadmin)
    second = await add(db, crypto, seed_data, host="ghcr.io")
    assert second.id > first.id


async def test_logins_open_every_token_in_host_order_and_never_show_it_in_a_repr(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    assert await load_registry_logins(db, crypto) == ()
    await add(db, crypto, seed_data, host="ghcr.io", username="ci", token="ghp_second-token")
    await add(db, crypto, seed_data, host="docker.io", username="bot", token=TOKEN)
    logins = await load_registry_logins(db, crypto)
    assert logins == (
        RegistryLogin("docker.io", "bot", TOKEN),
        RegistryLogin("ghcr.io", "ci", "ghp_second-token"),
    )
    for login in logins:
        assert login.token not in repr(login) and login.username in repr(login)
    assert TOKEN not in repr(logins)


async def test_a_token_that_does_not_open_is_a_clear_error_naming_the_registry(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    await add(db, crypto, seed_data, host="registry.example.com", token=TOKEN)
    await add(db, crypto, seed_data, host="ghcr.io", token=OTHER_TOKEN)
    other_key = CryptoService(parse_master_key(generate_master_key()))
    with pytest.raises(RegistryTokenError) as wrong_key:
        await load_registry_logins(db, other_key)
    assert wrong_key.value.host == "ghcr.io"
    assert "ghcr.io" in str(wrong_key.value) and "Settings" in str(wrong_key.value)

    first, second = await list_registries(db)
    second.token_encrypted = first.token_encrypted
    await db.commit()
    with pytest.raises(RegistryTokenError) as moved:
        await load_registry_logins(db, crypto)
    assert moved.value.host == second.host
    assert TOKEN not in str(moved.value) and OTHER_TOKEN not in str(moved.value)


def decoded(entry: dict[str, str]) -> tuple[str, str]:
    """The username and token inside an ``auth`` value, split at the first colon as Docker does."""
    username, _, token = base64.b64decode(entry["auth"]).decode().partition(":")
    return username, token


def test_the_docker_config_holds_one_auth_entry_for_a_registry() -> None:
    config = docker_config([RegistryLogin("registry.example.com:5000", "bot", TOKEN)])
    assert config == {
        "auths": {
            "registry.example.com:5000": {
                "auth": base64.b64encode(f"bot:{TOKEN}".encode()).decode()
            }
        }
    }
    assert json.loads(json.dumps(config)) == config


def test_the_docker_config_holds_an_entry_for_each_of_two_registries() -> None:
    config = docker_config(
        (
            RegistryLogin("ghcr.io", "ci", "ghp_one"),
            RegistryLogin("registry.example.com", "bot", "tok:with:colons"),
        )
    )
    assert list(config) == ["auths"]
    assert set(config["auths"]) == {"ghcr.io", "registry.example.com"}
    assert decoded(config["auths"]["ghcr.io"]) == ("ci", "ghp_one")
    assert decoded(config["auths"]["registry.example.com"]) == ("bot", "tok:with:colons")


@pytest.mark.parametrize("host", ["docker.io", "index.docker.io", "registry-1.docker.io"])
def test_the_docker_hub_names_use_the_key_docker_looks_docker_hub_up_by(host: str) -> None:
    config = docker_config([RegistryLogin(host, "me", "secret")])
    assert set(config["auths"]) == {DOCKER_HUB_KEY} == {"https://index.docker.io/v1/"}
    assert decoded(config["auths"][DOCKER_HUB_KEY]) == ("me", "secret")


def test_docker_hub_and_another_registry_each_get_their_own_key() -> None:
    config = docker_config(
        [RegistryLogin("docker.io", "me", "hub"), RegistryLogin("ghcr.io", "ci", "gh")]
    )
    assert set(config["auths"]) == {DOCKER_HUB_KEY, "ghcr.io"}


def test_a_config_without_logins_has_no_auths_and_the_first_of_two_with_one_key_wins() -> None:
    assert docker_config(()) == {"auths": {}}
    config = docker_config(
        [RegistryLogin("docker.io", "first", "one"), RegistryLogin("index.docker.io", "x", "two")]
    )
    assert decoded(config["auths"][DOCKER_HUB_KEY]) == ("first", "one")


async def test_the_config_of_the_stored_logins_has_every_registry_once(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData
) -> None:
    await add(db, crypto, seed_data, host="docker.io", username="me", token="hub-token")
    await add(db, crypto, seed_data, host="registry.example.com", username="bot", token=TOKEN)
    config = docker_config(await load_registry_logins(db, crypto))
    assert set(config["auths"]) == {DOCKER_HUB_KEY, "registry.example.com"}
    assert decoded(config["auths"]["registry.example.com"]) == ("bot", TOKEN)
    count = (await db.execute(select(func.count()).select_from(DockerRegistry))).scalar_one()
    assert count == 2
