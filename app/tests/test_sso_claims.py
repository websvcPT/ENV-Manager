"""Tests for reading the roles and the group from the claims of an ID token."""

import pytest

from app.models import UserGroup
from app.services.app_settings import SsoSettings
from app.services.sso_claims import claim_at_path, group_for, read_claims, roles_from
from app.services.sso_refusal import EMAIL_MISSING, TOKEN_INVALID, SsoRefusal

CLAIMS: dict[str, object] = {
    "resource_access": {
        "env-manager": {"roles": ["admin", "viewer"]},
        "my.app": {"roles": ["dotted"]},
    },
    "realm_access": {"roles": ["realm-role"]},
    "groups": ["a", "b"],
    "flat.key": {"inner": ["flat"]},
    "scalar": "text",
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("groups", ["a", "b"]),
        ("realm_access.roles", ["realm-role"]),
        ("resource_access.env-manager.roles", ["admin", "viewer"]),
        ("resource_access.my.app.roles", ["dotted"]),
        ("flat.key.inner", ["flat"]),
        ("scalar", "text"),
        ("scalar.deeper", None),
        ("resource_access.missing.roles", None),
        ("nothing", None),
        ("", None),
        (".", None),
        ("groups.0", None),
    ],
)
def test_a_dotted_path_walks_the_claims(path: str, expected: object) -> None:
    assert claim_at_path(CLAIMS, path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("groups", ("a", "b")),
        ("resource_access.my.app.roles", ("dotted",)),
        ("scalar", ()),
        ("nothing", ()),
        ("resource_access", ()),
    ],
)
def test_roles_are_a_list_of_texts_and_anything_else_is_none(
    path: str, expected: tuple[str, ...]
) -> None:
    assert roles_from(CLAIMS, path) == expected


def test_a_list_with_something_other_than_text_gives_no_roles() -> None:
    assert roles_from({"roles": ["admin", 3]}, "roles") == ()
    assert roles_from({"roles": [["admin"]]}, "roles") == ()
    assert roles_from({"roles": []}, "roles") == ()


def test_the_default_path_reads_the_roles_of_this_client() -> None:
    settings = SsoSettings(client_id="env-manager")
    assert settings.effective_roles_claim == "resource_access.env-manager.roles"
    assert roles_from(CLAIMS, settings.effective_roles_claim) == ("admin", "viewer")
    assert SsoSettings(roles_claim="groups").effective_roles_claim == "groups"


def test_the_first_row_with_a_role_the_person_has_decides_the_group() -> None:
    rows = (("ops", "viewer"), ("admin", "admin"), ("ops", "admin"))
    assert group_for(("admin", "ops"), rows) is UserGroup.VIEWER
    assert group_for(("admin",), rows) is UserGroup.ADMIN
    assert group_for(("other",), rows) is None
    assert group_for((), rows) is None
    assert group_for(("admin",), ()) is None


def test_a_role_matches_exactly_and_never_by_part_or_case() -> None:
    assert group_for(("Admin", "admins", "admin "), (("admin", "admin"),)) is None


def test_the_claims_a_sign_in_needs_are_read_with_their_defaults() -> None:
    settings = SsoSettings(client_id="env-manager")
    claims = read_claims(
        {
            "sub": "abc",
            "email": " pat@example.test ",
            "email_verified": True,
            "name": "Pat Example",
            "resource_access": {"env-manager": {"roles": ["admin"]}},
        },
        settings,
    )
    assert (claims.subject, claims.email, claims.email_verified, claims.name) == (
        "abc",
        "pat@example.test",
        True,
        "Pat Example",
    )
    assert claims.roles == ("admin",)


@pytest.mark.parametrize("value", [None, False, "true", 1, "yes"])
def test_only_a_true_email_verified_counts_as_verified(value: object) -> None:
    claims = read_claims(
        {"sub": "abc", "email": "pat@example.test", "email_verified": value}, SsoSettings()
    )
    assert claims.email_verified is False


@pytest.mark.parametrize("subject", [None, "", 12, ["x"], "s" * 256])
def test_a_subject_that_is_not_a_short_text_is_refused(subject: object) -> None:
    with pytest.raises(SsoRefusal) as info:
        read_claims({"sub": subject, "email": "pat@example.test"}, SsoSettings())
    assert info.value.reason == TOKEN_INVALID


@pytest.mark.parametrize("email", [None, "", 5, "pat", "pat@host"])
def test_an_email_that_is_not_an_address_is_refused(email: object) -> None:
    with pytest.raises(SsoRefusal) as info:
        read_claims({"sub": "abc", "email": email}, SsoSettings())
    assert info.value.reason == EMAIL_MISSING
