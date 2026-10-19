"""Tests for the extra notification addresses of a project: the typed list, saving it with its
audit entry, and the panel on the project settings tab."""

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditEntry, AuditResult, Project, User
from app.services.project_addresses import (
    MAX_ADDRESSES,
    AddressesError,
    parse_addresses,
    save_extra_addresses,
)
from app.tests.conftest import sign_in
from app.tests.settings_helpers import error_of, toast_of
from app.tests.web_helpers import assert_never_cached, post


def test_one_address_per_line_in_the_order_typed() -> None:
    """Blank lines and surrounding spaces do not count."""
    text = "  ops@example.com \n\n oncall@example.com\r\nteam@example.org\n"
    assert parse_addresses(text) == ["ops@example.com", "oncall@example.com", "team@example.org"]
    assert parse_addresses("") == [] and parse_addresses("  \n \n") == []


def test_commas_semicolons_and_spaces_separate_addresses_too() -> None:
    """A pasted list works."""
    assert parse_addresses("a@example.com, b@example.com;c@example.com d@example.com") == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
        "d@example.com",
    ]


def test_an_address_typed_twice_is_kept_once_with_the_first_spelling() -> None:
    """Duplicates are found whatever the case."""
    found = parse_addresses("Ops@Example.com\nb@example.com\nops@example.COM\nOPS@example.com")
    assert found == ["Ops@Example.com", "b@example.com"]


def test_private_names_such_as_corp_local_are_plain_addresses() -> None:
    """The same address rule as the SMTP from address and the recipients."""
    assert parse_addresses("ops@corp.local\nbuild@example.test") == [
        "ops@corp.local",
        "build@example.test",
    ]


@pytest.mark.parametrize(
    "typed",
    [
        "not-an-address",
        "ops@",
        "@example.com",
        "ops@example",
        "Ops <ops@example.com>",
        "ops@example.com>",
        "ops@exa mple.com",
        "oéps@example.com",
        "ops@@example.com",
        "ops@example..com",
    ],
)
def test_text_that_is_not_one_plain_address_is_refused_by_name(typed: str) -> None:
    """The first bad entry is named so it can be found."""
    with pytest.raises(AddressesError) as caught:
        parse_addresses(f"fine@example.com\n{typed}")
    assert str(caught.value).endswith(" is not a valid address.")
    assert "fine@example.com" not in str(caught.value)


def test_a_very_long_entry_is_shortened_in_the_message() -> None:
    """The reason never echoes more than a short start."""
    with pytest.raises(AddressesError) as caught:
        parse_addresses("a" * 300 + "@example.com")
    assert len(str(caught.value)) < 120 and "..." in str(caught.value)


def test_twenty_addresses_are_the_most() -> None:
    """The count is bounded after duplicates are removed."""
    twenty = [f"user{number}@example.com" for number in range(MAX_ADDRESSES)]
    assert parse_addresses("\n".join(twenty)) == twenty
    assert parse_addresses("\n".join([*twenty, twenty[0].upper()])) == twenty
    with pytest.raises(AddressesError, match="at most 20"):
        parse_addresses("\n".join([*twenty, "one-more@example.com"]))


def test_an_unreasonably_long_text_is_refused_before_it_is_read() -> None:
    """The text is bounded as well."""
    with pytest.raises(AddressesError, match="too long"):
        parse_addresses("a@example.com\n" * 1000)


async def test_saving_stores_the_list_and_audits_what_was_added_and_removed(
    web_app: FastAPI, fancy: int
) -> None:
    """The entry names the project and the addresses and has no item scope."""
    async with web_app.state.session_factory() as db:
        project = await db.get(Project, fancy)
        actor = await db.scalar(select(User).where(User.name == "root"))
        assert actor is not None
        assert project is not None and project.extra_addresses == []
        first = await save_extra_addresses(
            db, project, "Ops@example.com\nb@example.com", actor=actor, ip_address="203.0.113.5"
        )
        assert first.changed and first.addresses == ["Ops@example.com", "b@example.com"]
        second = await save_extra_addresses(
            db, project, "Ops@example.com\nb@example.com\nc@example.com", actor=actor
        )
        assert second.changed
        third = await save_extra_addresses(db, project, "b@example.com\nc@example.com", actor=actor)
        assert third.changed
        same = await save_extra_addresses(db, project, "b@example.com\nc@example.com", actor=actor)
        assert same.changed is False
    async with web_app.state.session_factory() as db:
        stored = await db.scalar(select(Project.extra_addresses).where(Project.id == fancy))
        entries = list(
            (
                await db.execute(
                    select(AuditEntry)
                    .where(AuditEntry.action == "project.notification_addresses")
                    .order_by(AuditEntry.id)
                )
            ).scalars()
        )
    assert stored == ["b@example.com", "c@example.com"]
    assert [entry.details for entry in entries] == [
        {"added": ["Ops@example.com", "b@example.com"], "removed": [], "count": 2},
        {"added": ["c@example.com"], "removed": [], "count": 3},
        {"added": [], "removed": ["Ops@example.com"], "count": 2},
    ]
    for entry in entries:
        assert entry.result is AuditResult.SUCCESS and entry.actor_name == "root"
        assert entry.target_type == "project" and entry.target_name == "Fancy Proj. Name"
        assert (entry.project_id, entry.deployment_id, entry.service_id) == (None, None, None)
    assert entries[0].ip_address == "203.0.113.5"


async def test_a_refused_list_changes_and_audits_nothing(web_app: FastAPI, fancy: int) -> None:
    """The stored list stays as it was."""
    async with web_app.state.session_factory() as db:
        project = await db.get(Project, fancy)
        assert project is not None
        actor = await db.scalar(select(User).where(User.name == "root"))
        assert actor is not None
        await save_extra_addresses(db, project, "ops@example.com", actor=actor)
        with pytest.raises(AddressesError):
            await save_extra_addresses(db, project, "x@example.com\nbad", actor=actor)
        await db.refresh(project)
        assert project.extra_addresses == ["ops@example.com"]
        count = len(
            (
                await db.execute(
                    select(AuditEntry).where(AuditEntry.action == "project.notification_addresses")
                )
            ).all()
        )
    assert count == 1


def panel_of(page: str) -> str:
    """The markup of the addresses panel."""
    start = page.index('data-fragment="project-addresses"')
    return page[start : page.index("</section>", start)]


@pytest.fixture
async def fancy(web_app: FastAPI) -> int:
    """The id of the seeded Fancy Proj. Name project."""
    async with web_app.state.session_factory() as db:
        found = await db.scalar(select(Project.id).where(Project.name == "Fancy Proj. Name"))
    assert found is not None
    return int(found)


async def test_the_settings_tab_has_the_addresses_panel_between_rename_and_delete(
    web_client: httpx.AsyncClient, fancy: int
) -> None:
    """An empty textarea, its hint and a save button; the panel sits above the danger zone."""
    await sign_in(web_client, "root")
    response = await web_client.get(f"/projects/{fancy}/settings")
    assert_never_cached(response)
    page = response.text
    assert (
        page.index('data-fragment="project-rename"')
        < page.index('data-fragment="project-addresses"')
        < page.index('id="delete-zone-title"')
    )
    panel = panel_of(page)
    assert 'id="addresses-title"' in panel and "Notification addresses" in panel
    assert 'name="addresses"' in panel and "Extra addresses, one per line" in panel
    assert f'hx-post="/projects/{fancy}/notification-addresses"' in panel
    assert "Save addresses" in panel and "At most 20 addresses." in panel


async def test_saving_the_addresses_stores_them_and_shows_the_cleaned_list(
    web_client: httpx.AsyncClient, web_app: FastAPI, fancy: int
) -> None:
    """Duplicates collapse, the toast says saved and a reload shows the same list."""
    await sign_in(web_client, "root")
    typed = "Ops@Example.com\n\nops@example.com\nteam@example.org"
    response = await post(
        web_client, f"/projects/{fancy}/notification-addresses", {"addresses": typed}
    )
    assert response.status_code == 200
    assert toast_of(response.text) == ("Addresses saved", "They apply to the next notification.")
    assert 'data-fragment="project-addresses"' in response.text
    assert ">Ops@Example.com\nteam@example.org</textarea>" in response.text
    async with web_app.state.session_factory() as db:
        stored = await db.scalar(select(Project.extra_addresses).where(Project.id == fancy))
    assert stored == ["Ops@Example.com", "team@example.org"]
    page = (await web_client.get(f"/projects/{fancy}/settings")).text
    assert ">Ops@Example.com\nteam@example.org</textarea>" in page
    again = await post(
        web_client,
        f"/projects/{fancy}/notification-addresses",
        {"addresses": "Ops@Example.com\nteam@example.org"},
    )
    assert toast_of(again.text) == ("Nothing to save", "The list is unchanged.")


async def test_a_refused_list_answers_with_the_reason_and_keeps_what_was_typed(
    web_client: httpx.AsyncClient, web_app: FastAPI, fancy: int
) -> None:
    """The panel comes back (422) with the message under the field and nothing stored."""
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"/projects/{fancy}/notification-addresses",
        {"addresses": "ok@example.com\nnot-an-address"},
    )
    assert response.status_code == 422
    assert error_of(response.text, "xa") == "not-an-address is not a valid address."
    assert ">ok@example.com\nnot-an-address</textarea>" in response.text
    assert 'aria-invalid="true"' in response.text
    async with web_app.state.session_factory() as db:
        assert await db.scalar(select(Project.extra_addresses).where(Project.id == fancy)) == []


async def test_only_the_superadmin_reaches_the_addresses_and_other_projects_stay_hidden(
    web_client: httpx.AsyncClient, web_app: FastAPI, fancy: int
) -> None:
    """403 for a project in scope, 404 for one outside it, and nothing stored."""
    async with web_app.state.session_factory() as db:
        billing = await db.scalar(select(Project.id).where(Project.name == "billing"))
    await sign_in(web_client, "project_admin")
    assert (await web_client.get(f"/projects/{fancy}/settings")).status_code == 403
    sent = await post(
        web_client, f"/projects/{fancy}/notification-addresses", {"addresses": "a@example.com"}
    )
    assert sent.status_code == 403
    outside = await post(
        web_client, f"/projects/{billing}/notification-addresses", {"addresses": "a@example.com"}
    )
    assert outside.status_code == 404
    page = (await web_client.get(f"/projects/{fancy}")).text
    assert "project-addresses" not in page and "notification-addresses" not in page
    async with web_app.state.session_factory() as db:
        stored = (await db.scalars(select(Project.extra_addresses))).all()
    assert all(value == [] for value in stored)
