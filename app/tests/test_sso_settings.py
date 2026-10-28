"""Tests for the single sign-on settings: defaults, storage of the mapping rows, the form checks
and saving it with its secret."""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import AppSetting, AuditEntry
from app.services.app_settings import (
    SECRET_KEYS,
    SSO_CLIENT_SECRET_KEY,
    SsoSettings,
    has_secret,
    load_stored_settings,
    read_secret,
    read_section,
)
from app.services.audit import AuditActor
from app.services.sso_provider import SsoProvider
from app.services.sso_refusal import (
    ISSUER_MISMATCH,
    MESSAGES,
    PROVIDER_INVALID,
    PROVIDER_UNAVAILABLE,
)
from app.services.sso_settings import (
    DiscoveryCheck,
    SsoForm,
    SsoFormError,
    check_discovery,
    review_sso,
    save_sso,
    validate_sso,
)
from app.tests.sso_helpers import ISSUER, FakeIdp, ec_key, rsa_key

SECRET = "the-client-secret-0123456789"


def form(**changes: object) -> SsoForm:
    """A valid form, with fields replaced."""
    values: dict[str, object] = {
        "enabled": True,
        "issuer": "https://sso.example.com/realms/main",
        "client_id": "env-manager",
        "roles_claim": "",
        "account_url": "",
        "create_users": True,
        "link_by_email": True,
        "mappings": (("env-admin", "admin"), ("env-viewer", "viewer")),
        "client_secret": "",
        "remove_secret": False,
    }
    values.update(changes)
    return SsoForm(**values)  # type: ignore[arg-type]


def refused(**changes: object) -> dict[str, str]:
    """The messages by field for a form that is refused."""
    with pytest.raises(SsoFormError) as error:
        validate_sso(form(**changes))
    return error.value.errors


def test_the_defaults_are_off_with_creating_and_linking_on() -> None:
    settings = SsoSettings()
    assert (settings.enabled, settings.create_users, settings.link_by_email) == (False, True, True)
    assert settings.mappings == () and not settings.configured
    assert SSO_CLIENT_SECRET_KEY in SECRET_KEYS


def test_the_claim_path_and_the_account_address_default_from_the_client_and_the_issuer() -> None:
    settings = SsoSettings(issuer="https://sso.example.com/realms/main/", client_id="env-manager")
    assert settings.effective_roles_claim == "resource_access.env-manager.roles"
    assert settings.effective_account_url == "https://sso.example.com/realms/main/account"
    custom = SsoSettings(roles_claim="groups", account_url="https://me.example.com/")
    assert (custom.effective_roles_claim, custom.effective_account_url) == (
        "groups",
        "https://me.example.com/",
    )


def test_it_is_configured_only_when_on_with_an_issuer_and_a_client() -> None:
    on = SsoSettings(enabled=True, issuer="https://a.example.com", client_id="c")
    assert on.configured
    assert not SsoSettings(enabled=False, issuer="https://a.example.com", client_id="c").configured
    assert not SsoSettings(enabled=True, client_id="c").configured
    assert not SsoSettings(enabled=True, issuer="https://a.example.com").configured


def test_a_valid_form_becomes_the_settings_and_trims_its_text() -> None:
    clean = validate_sso(
        form(
            issuer=" https://sso.example.com/realms/main ",
            client_id=" env-manager ",
            mappings=((" env-admin ", "admin"),),
            roles_claim=" groups ",
        )
    )
    assert clean.settings == SsoSettings(
        True,
        "https://sso.example.com/realms/main",
        "env-manager",
        "groups",
        "",
        True,
        True,
        (("env-admin", "admin"),),
    )
    assert (clean.client_secret, clean.remove_secret) == ("", False)


def test_switched_off_the_form_may_be_empty() -> None:
    clean = validate_sso(form(enabled=False, issuer="", client_id="", mappings=()))
    assert clean.settings == SsoSettings(enabled=False, create_users=True, link_by_email=True)


@pytest.mark.parametrize(
    "issuer",
    [
        "sso.example.com",
        "ftp://sso.example.com",
        "https://",
        "https://sso.example.com/realms/main?x=1",
        "https://sso.example.com/realms/main#frag",
        "https://user:pass@sso.example.com",
        "https://sso.example.com:99999",
        "https://sso.example.com/a b",
        "https://sso.example.com/é",
        "javascript:alert(1)",
        "https://sso.example.com/" + "a" * 2100,
    ],
)
def test_the_issuer_is_an_absolute_http_or_https_address_without_query_or_fragment(
    issuer: str,
) -> None:
    assert "issuer" in refused(issuer=issuer)


@pytest.mark.parametrize(
    "issuer",
    [
        "http://localhost:8180/realms/dev",
        "https://sso.example.com",
        "https://sso.example.com/",
        "http://192.168.10.181:8180/realms/main",
        "https://[::1]:8443/realms/x",
    ],
)
def test_http_and_https_issuers_are_accepted(issuer: str) -> None:
    assert validate_sso(form(issuer=issuer)).settings.issuer == issuer


def test_the_issuer_and_the_client_are_required_when_switched_on() -> None:
    errors = refused(issuer="", client_id=" ")
    assert set(errors) == {"issuer", "client_id"}


@pytest.mark.parametrize(
    "claim",
    ["roles", "resource_access.env-manager.roles", "a.b.c", "realm-access.roles"],
)
def test_a_dotted_path_is_a_roles_claim(claim: str) -> None:
    assert validate_sso(form(roles_claim=claim)).settings.roles_claim == claim


@pytest.mark.parametrize("claim", ["a..b", ".a", "a.", "a b", "a. b", "a\tb", "x" * 300, "."])
def test_a_claim_that_is_not_a_dotted_path_is_refused(claim: str) -> None:
    assert "roles_claim" in refused(roles_claim=claim)


def test_the_account_address_is_optional_and_otherwise_an_http_address() -> None:
    assert validate_sso(form(account_url="https://me.example.com/account?x=1")).settings.account_url
    assert "account_url" in refused(account_url="me.example.com")
    assert "account_url" in refused(account_url="javascript:alert(1)")


@pytest.mark.parametrize(
    "mappings",
    [
        (("", "admin"),),
        (("  ", "admin"),),
        (("env-admin", "superadmin"),),
        (("env-admin", ""),),
        (("env-admin", "Admin"),),
        (("env-admin", "owner"),),
        (("env-admin", "admin"), ("x", "superadmin")),
        (("x" * 256, "admin"),),
        (("tab\there", "admin"),),
    ],
)
def test_a_row_needs_a_role_and_the_group_admin_or_viewer(
    mappings: tuple[tuple[str, str], ...],
) -> None:
    assert "mappings" in refused(mappings=mappings)


def test_the_message_names_the_first_bad_row() -> None:
    message = refused(mappings=(("a", "admin"), ("", "viewer")))["mappings"]
    assert message.startswith("Row 2")


def test_there_is_a_limit_on_the_number_of_rows() -> None:
    rows = tuple((f"role-{number}", "viewer") for number in range(51))
    assert "mappings" in refused(mappings=rows)
    assert validate_sso(form(mappings=rows[:50])).settings.mappings == rows[:50]


def test_duplicate_roles_are_kept_in_their_order() -> None:
    rows = (("ops", "viewer"), ("ops", "admin"))
    assert validate_sso(form(mappings=rows)).settings.mappings == rows


def test_a_secret_is_checked_and_cannot_be_typed_and_removed_at_once() -> None:
    assert "client_secret" in refused(client_secret="a\nb")
    assert "client_secret" in refused(client_secret="x" * 1025)
    assert "client_secret" in refused(client_secret=SECRET, remove_secret=True)
    clean = validate_sso(form(client_secret=SECRET))
    assert (clean.client_secret, clean.remove_secret) == (SECRET, False)


def test_a_form_shows_the_stored_settings_with_an_empty_secret() -> None:
    rows = (("r", "viewer"),)
    settings = SsoSettings(True, "https://a.example.com", "c", "g", "", False, True, rows)
    shown = SsoForm.from_settings(settings)
    assert shown.client_secret == "" and not shown.remove_secret
    assert validate_sso(shown).settings == settings


def test_stored_mapping_rows_come_back_as_pairs() -> None:
    stored = {"sso.mappings": json.dumps([["a", "admin"], ["b", "viewer"]])}
    assert read_section("sso", SsoSettings, stored).mappings == (("a", "admin"), ("b", "viewer"))


@pytest.mark.parametrize(
    "raw",
    ['"text"', "[[1, 2]]", '[["a"]]', '[["a", "b", "c"]]', "{}", '["a"]', "not json"],
)
def test_stored_rows_of_the_wrong_shape_read_as_no_rows(raw: str) -> None:
    assert read_section("sso", SsoSettings, {"sso.mappings": raw}).mappings == ()


async def test_saving_stores_the_settings_and_the_secret_and_audits_without_the_secret(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        changed = await save_sso(
            db, crypto, validate_sso(form(client_secret=SECRET)), actor=AuditActor.cli()
        )
    assert changed
    async with factory() as db:
        stored = await load_stored_settings(db)
        assert stored.sso == validate_sso(form()).settings
        assert SSO_CLIENT_SECRET_KEY in stored.secrets
        assert await has_secret(db, SSO_CLIENT_SECRET_KEY)
        assert await read_secret(db, crypto, SSO_CLIENT_SECRET_KEY) == SECRET
        raw = await db.get(AppSetting, SSO_CLIENT_SECRET_KEY)
        assert raw is not None and SECRET not in raw.value
        entries = (await db.execute(select(AuditEntry))).scalars().all()
    text = json.dumps([entry.details for entry in entries])
    assert SECRET not in text
    assert any(entry.details.get("section") == "sso" for entry in entries)
    changes = [c for entry in entries for c in entry.details.get("changes", [])]  # type: ignore[attr-defined]
    assert {"setting": SSO_CLIENT_SECRET_KEY, "kind": "secret"} in changes
    rows = next(c for c in changes if c["setting"] == "sso.mappings")
    assert rows["new"] == [["env-admin", "admin"], ["env-viewer", "viewer"]]


async def test_an_empty_secret_field_keeps_the_stored_secret_and_remove_deletes_it(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        await save_sso(db, crypto, validate_sso(form(client_secret=SECRET)), actor=AuditActor.cli())
    async with factory() as db:
        changed = await save_sso(db, crypto, validate_sso(form()), actor=AuditActor.cli())
        assert not changed
        assert await read_secret(db, crypto, SSO_CLIENT_SECRET_KEY) == SECRET
    async with factory() as db:
        changed = await save_sso(
            db, crypto, validate_sso(form(remove_secret=True)), actor=AuditActor.cli()
        )
        assert changed
        assert not await has_secret(db, SSO_CLIENT_SECRET_KEY)
        entries = (await db.execute(select(AuditEntry))).scalars().all()
    assert SECRET not in json.dumps([entry.details for entry in entries])


async def test_saving_the_same_settings_again_writes_no_audit_entry(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    factory = create_session_factory(engine)
    for _ in range(2):
        async with factory() as db:
            await save_sso(db, crypto, validate_sso(form()), actor=AuditActor.cli())
    async with factory() as db:
        entries = (await db.execute(select(AuditEntry))).scalars().all()
    assert len(entries) == 1


def reviewed(
    *, secret_stored: bool = False, was_on: bool = False, confirmed: bool = True, **changes: object
) -> dict[str, str]:
    """The messages by field for a form that review_sso refuses."""
    with pytest.raises(SsoFormError) as error:
        review_sso(form(**changes), secret_stored=secret_stored, was_on=was_on, confirmed=confirmed)
    return error.value.errors


def test_switching_on_needs_a_secret_typed_or_stored_and_not_removed() -> None:
    complete = "Complete the settings below before turning single sign-on on."
    assert reviewed() == {"client_secret": "Enter the client secret.", "enabled": complete}
    assert reviewed(secret_stored=True, remove_secret=True) == {
        "client_secret": "Enter the client secret.",
        "enabled": complete,
    }
    assert review_sso(form(), secret_stored=True, was_on=False, confirmed=True).settings.enabled
    assert review_sso(
        form(client_secret=SECRET), secret_stored=False, was_on=False, confirmed=True
    ).settings.enabled


def test_switching_on_needs_a_mapping_row_and_a_stored_off_form_needs_none() -> None:
    assert reviewed(secret_stored=True, mappings=())["mappings"] == "Add at least one mapping row."
    off = review_sso(
        form(enabled=False, mappings=()), secret_stored=False, was_on=False, confirmed=False
    )
    assert off.settings.mappings == () and not off.settings.enabled


def test_a_save_that_keeps_sign_in_on_is_checked_like_switching_it_on() -> None:
    assert "client_secret" in reviewed(was_on=True, secret_stored=False)
    assert "mappings" in reviewed(was_on=True, secret_stored=True, mappings=())


def test_the_first_bad_field_message_is_kept_beside_the_enabled_one() -> None:
    errors = reviewed(secret_stored=True, issuer="", mappings=(("", "viewer"),))
    assert errors["issuer"] == "Enter the issuer address of the provider."
    assert errors["mappings"].startswith("Row 1: enter the role")
    assert errors["enabled"].startswith("Complete the settings")


def test_switching_on_from_off_needs_the_confirmation() -> None:
    errors = reviewed(secret_stored=True, confirmed=False)
    assert errors == {"enabled": "Turning single sign-on on needs a confirmation."}
    assert review_sso(form(), secret_stored=True, was_on=True, confirmed=False).settings.enabled


async def test_a_discovery_test_counts_the_signing_keys_and_stores_nothing() -> None:
    idp = FakeIdp(keys=[rsa_key("key-1"), ec_key("key-2")])
    check = await check_discovery(SsoProvider(idp.transport()), f" {ISSUER} ")
    assert check == DiscoveryCheck(True, "Discovery worked: 2 signing keys.", ISSUER, 2)
    one = await check_discovery(SsoProvider(FakeIdp().transport()), ISSUER)
    assert one.message == "Discovery worked: 1 signing key."


async def test_a_discovery_test_fetches_again_instead_of_answering_from_the_cache() -> None:
    idp = FakeIdp()
    provider = SsoProvider(idp.transport())
    assert (await check_discovery(provider, ISSUER)).ok
    idp.down = True
    check = await check_discovery(provider, ISSUER)
    assert (check.ok, check.message) == (False, MESSAGES[PROVIDER_UNAVAILABLE])


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"down": True}, PROVIDER_UNAVAILABLE),
        ({"discovery_issuer": "https://other.example.test"}, ISSUER_MISMATCH),
        ({"keys": []}, PROVIDER_INVALID),
    ],
)
async def test_a_failed_discovery_test_is_one_fixed_sentence(
    changes: dict[str, object], reason: str
) -> None:
    check = await check_discovery(SsoProvider(FakeIdp(**changes).transport()), ISSUER)  # type: ignore[arg-type]
    assert (check.ok, check.message, check.issuer) == (False, MESSAGES[reason], ISSUER)


async def test_a_discovery_test_refuses_an_empty_or_malformed_issuer_without_a_request() -> None:
    idp = FakeIdp()
    provider = SsoProvider(idp.transport())
    assert (await check_discovery(provider, "  ")).message == "Enter the issuer address first."
    bad = await check_discovery(provider, "ftp://idp.example.test")
    assert not bad.ok and bad.message == "Enter an http or https address that names a host."
    assert idp.seen == []
