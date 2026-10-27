"""The Settings screens: who may open them, every tab, saving and what a saved value changes."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import VERSION
from app.models import (
    AuditActorType,
    AuditEntry,
    AuditResult,
    Deployment,
    Project,
    UserSession,
)
from app.services import app_settings
from app.services.app_settings import StoredSettings
from app.tests.conftest import sign_in
from app.tests.fake_docker import FakeDocker
from app.tests.settings_helpers import (
    NON_SUPERADMINS,
    TABS,
    browser_as,
    changes_of,
    error_of,
    selected_of,
    settings_entries,
    stored,
    stored_rows,
    toast_of,
    value_of,
)
from app.tests.web_helpers import assert_never_cached, post

SECURITY_FORM = {
    "idle_timeout_minutes": "30",
    "absolute_timeout_hours": "12",
    "user_max_failures": "5",
    "ip_max_failures": "20",
    "api_ip_max_failures": "10",
    "lockout_minutes": "15",
    "min_password_length": "12",
}
GENERAL_FORM = {
    "time_zone": "UTC",
    "date_format": "iso",
    "audit_retention_days": "90",
    "dist_missing_policy": "block",
    "drift_policy": "halt",
    "missing_target_policy": "create",
    "file_mode": "0600",
    "file_owner": "",
    "file_group": "",
}


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


async def test_only_the_superadmin_opens_the_settings(web_app: FastAPI) -> None:
    """Every other group gets 403 on every tab and on every change, and nothing is stored."""
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            for path in ("/settings", *(f"/settings/{tab}" for tab in TABS)):
                assert (await http.get(path)).status_code == 403, (user, path)
            for path, data in (
                ("/settings/security", SECURITY_FORM),
                ("/settings/general", GENERAL_FORM),
                ("/settings/post-deploy", {"enabled": "on"}),
                ("/settings/paths", {"path": "/opt/example"}),
            ):
                assert (await post(http, path, data)).status_code == 403, (user, path)
            assert (await http.get("/settings/paths/validate?path=/opt/example")).status_code == 403
            assert (await http.get("/settings/paths/new")).status_code == 403
    assert await stored_rows(web_app) == {}
    assert await settings_entries(web_app) == []


async def test_an_anonymous_visitor_is_sent_to_sign_in(web_client: httpx.AsyncClient) -> None:
    """Signed out, the settings answer like every other screen."""
    response = await web_client.get("/settings/security")
    assert (response.status_code, response.headers["location"]) == (303, "/login")


async def test_the_settings_address_opens_the_first_tab(root: httpx.AsyncClient) -> None:
    """The sidebar links to /settings, which answers with the allowed paths."""
    response = await root.get("/settings")
    assert (response.status_code, response.headers["location"]) == (303, "/settings/paths")


@pytest.mark.parametrize("tab", TABS)
async def test_every_tab_has_the_header_and_the_nine_tabs(
    root: httpx.AsyncClient, tab: str
) -> None:
    """Each tab shows the Settings header, all nine tab links and marks its own."""
    response = await root.get(f"/settings/{tab}")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert ">Settings</h1>" in page
    assert "Application settings. Superadmin only." in page
    nav = re.search(r'<nav aria-label="Sections".*?</nav>', page, re.DOTALL)
    assert nav is not None
    links = re.findall(r'href="(/settings/[^"]+)"', nav.group(0))
    assert links == [f"/settings/{key}" for key in TABS]
    current = re.findall(r'<a href="([^"]+)" aria-current="page"', nav.group(0))
    assert current == [f"/settings/{tab}"]
    assert '<a href="/settings" title="Settings" aria-current="page"' in page


async def test_the_security_tab_shows_the_stored_values_and_the_policy_read_only(
    root: httpx.AsyncClient,
) -> None:
    """Defaults appear in the fields; the two-factor policy shows Optional and cannot change."""
    page = (await root.get("/settings/security")).text
    assert [value_of(page, name) for name in ("si", "sa", "sl", "sg", "sk", "sm", "sp")] == [
        "30",
        "12",
        "5",
        "20",
        "10",
        "15",
        "12",
    ]
    assert re.search(r'<input id="sg"[^>]*? disabled(?=[\s>])', page) is None
    policy = re.search(r"<div [^>]*data-two-factor-policy>(.*?)</div>", page, re.DOTALL)
    assert policy is not None
    text = " ".join(re.sub(r"<[^>]+>", " ", policy.group(1)).split())
    assert text == (
        "Two-factor authentication policy Optional Users can enable TOTP in their account."
    )
    assert "<input" not in policy.group(1) and 'name="tfa"' not in page
    assert "decided policy" not in page


async def test_saving_the_security_tab_stores_audits_and_applies_at_once(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A save is stored, audited with old and new values and read by the next request."""
    form = {**SECURITY_FORM, "idle_timeout_minutes": "45", "min_password_length": "14"}
    response = await post(root, "/settings/security", form)
    assert response.status_code == 200
    assert_never_cached(response)
    assert toast_of(response.text) == ("Security settings saved", "They apply now.")
    assert value_of(response.text, "si") == "45"
    current = (await stored(web_app)).security
    assert (current.idle_timeout_minutes, current.min_password_length) == (45, 14)
    (entry,) = await settings_entries(web_app)
    assert entry.action == "settings.update" and entry.actor_name == "root"
    assert entry.details == {
        "section": "security",
        "changes": [
            {"setting": "security.idle_timeout_minutes", "old": 30, "new": 45},
            {"setting": "security.min_password_length", "old": 12, "new": 14},
        ],
    }
    assert value_of((await root.get("/settings/security")).text, "sp") == "14"


async def test_saving_nothing_new_says_so_and_writes_nothing(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The same values again give a notice and no audit entry."""
    response = await post(root, "/settings/security", SECURITY_FORM)
    assert toast_of(response.text) == ("Nothing to save", "The settings are unchanged.")
    assert await settings_entries(web_app) == []


@pytest.mark.parametrize(
    ("field", "bad", "message"),
    [
        ("idle_timeout_minutes", "soon", "Enter a whole number from 5 to 1440."),
        ("idle_timeout_minutes", "4", "Enter a whole number from 5 to 1440."),
        ("absolute_timeout_hours", "0", "Enter a whole number from 1 to 168."),
        ("user_max_failures", "-3", "Enter a whole number from 1 to 1000."),
        ("ip_max_failures", "20.5", "Enter a whole number from 1 to 10000."),
        ("api_ip_max_failures", "", "Enter a whole number from 1 to 10000."),
        ("lockout_minutes", "99999999", "Enter a whole number from 1 to 1440."),
        ("min_password_length", "7", "Enter a whole number from 8 to 128."),
    ],
)
async def test_a_refused_security_value_shows_its_reason_and_stores_nothing(
    root: httpx.AsyncClient, web_app: FastAPI, field: str, bad: str, message: str
) -> None:
    """A value out of range or not a whole number is a 422 with the field's message."""
    ids = {
        "idle_timeout_minutes": "si",
        "absolute_timeout_hours": "sa",
        "user_max_failures": "sl",
        "ip_max_failures": "sg",
        "api_ip_max_failures": "sk",
        "lockout_minutes": "sm",
        "min_password_length": "sp",
    }
    response = await post(root, "/settings/security", {**SECURITY_FORM, field: bad})
    assert response.status_code == 422
    assert error_of(response.text, ids[field]) == message
    assert value_of(response.text, ids[field]) == bad
    assert toast_of(response.text) is None
    assert await settings_entries(web_app) == []
    assert await stored_rows(web_app) == {}


async def test_every_problem_is_listed_at_once_and_the_idle_limit_follows_the_absolute(
    root: httpx.AsyncClient,
) -> None:
    """Two bad fields give two messages; an idle timeout longer than the session is refused."""
    response = await post(
        root,
        "/settings/security",
        {**SECURITY_FORM, "user_max_failures": "x", "lockout_minutes": "0"},
    )
    assert error_of(response.text, "sl") and error_of(response.text, "sm")
    longer = await post(
        root,
        "/settings/security",
        {**SECURITY_FORM, "idle_timeout_minutes": "180", "absolute_timeout_hours": "2"},
    )
    assert longer.status_code == 422
    message = "The idle timeout cannot be longer than the absolute timeout."
    assert error_of(longer.text, "si") == message


async def test_the_lockout_threshold_applies_to_the_next_sign_in(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """With two failures allowed per user, the second wrong password locks the account."""
    await post(root, "/settings/security", {**SECURITY_FORM, "user_max_failures": "2"})
    transport = httpx.ASGITransport(app=web_app, client=("198.51.100.20", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as stranger:
        await stranger.get("/healthz")
        csrf = stranger.cookies["csrftoken"]
        statuses = []
        for _ in range(2):
            attempt = await stranger.post(
                "/login",
                data={"identifier": "viewer@example.test", "password": "wrong", "csrf_token": csrf},
            )
            statuses.append(attempt.status_code)
    assert statuses == [401, 429]


async def test_the_idle_timeout_applies_to_the_next_request(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A shorter idle timeout ends a session that was last seen longer ago than it allows."""
    await post(root, "/settings/security", {**SECURITY_FORM, "idle_timeout_minutes": "5"})
    async with web_app.state.session_factory() as db:
        await db.execute(
            update(UserSession).values(last_seen_at=datetime.now(UTC) - timedelta(minutes=6))
        )
        await db.commit()
    response = await root.get("/projects")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


async def test_the_minimum_password_length_applies_to_the_next_change(
    root: httpx.AsyncClient,
) -> None:
    """A new password shorter than the stored minimum is refused on the account page."""
    await post(root, "/settings/security", {**SECURITY_FORM, "min_password_length": "20"})
    response = await post(
        root,
        "/account/password",
        {
            "current_password": "dev-only-password",
            "new_password": "fifteen-chars-ok",
            "confirm_password": "fifteen-chars-ok",
        },
    )
    assert response.status_code == 422
    assert "The new password must be at least 20 characters." in response.text


@pytest.fixture
def raised_address_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the application with the development override of the per-address limit."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    monkeypatch.setenv("ENVMANAGER_LOGIN_IP_MAX_FAILURES", "60")


async def test_the_startup_override_fixes_the_address_limit_and_beats_the_stored_value(
    raised_address_limit: None, web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The field shows the override and cannot be edited; a posted value never changes it."""
    await sign_in(web_client, "root")
    page = (await web_client.get("/settings/security")).text
    assert value_of(page, "sg") == "60"
    assert re.search(r'<input id="sg"[^>]*? disabled(?=[\s>])', page)
    assert "ENVMANAGER_LOGIN_IP_MAX_FAILURES" in page and "development and testing only" in page

    response = await post(
        web_client,
        "/settings/security",
        {**SECURITY_FORM, "ip_max_failures": "7", "user_max_failures": "6"},
    )
    assert response.status_code == 200
    assert value_of(response.text, "sg") == "60"
    current = (await stored(web_app)).security
    assert (current.ip_max_failures, current.user_max_failures) == (20, 6)
    (entry,) = await settings_entries(web_app)
    assert [change["setting"] for change in changes_of(entry)] == ["security.user_max_failures"]


async def test_one_request_reads_the_stored_settings_once(
    root: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy, the page and the display settings share one read of the stored settings."""
    calls: list[int] = []
    original = app_settings.load_stored_settings

    async def counting(db: AsyncSession) -> StoredSettings:
        calls.append(1)
        return await original(db)

    monkeypatch.setattr("app.web.dependencies.load_stored_settings", counting)
    assert (await root.get("/settings/security")).status_code == 200
    assert len(calls) == 1


async def test_the_post_deploy_tab_without_a_socket_is_off_and_refuses_saves(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """With no Docker socket the status says so, the controls are disabled and a save is refused."""
    page = (await root.get("/settings/post-deploy")).text
    assert "Post-deploy commands are off because the Docker socket is not mounted." in page
    assert "no-docker.sock" in page
    assert re.search(r'<tr id="docker-socket">.*?Not mounted', page, re.DOTALL)
    assert re.search(r'<tr id="docker-daemon">.*?Not checked', page, re.DOTALL)
    assert re.search(r'<tr id="docker-compose">.*?Unknown', page, re.DOTALL)
    assert re.search(r'<input id="pd-enabled"[^>]*? disabled(?=[\s>])', page)
    assert "checked" not in re.search(r'<input id="pd-enabled"[^>]*>', page).group(0)  # type: ignore[union-attr]
    assert re.search(r'<button type="submit" disabled', page)

    response = await post(root, "/settings/post-deploy", {"enabled": "on"})
    assert response.status_code == 409
    assert "The Docker socket is not mounted." in response.text
    assert "Post-deploy commands are off, so this setting cannot be changed." in response.text
    assert toast_of(response.text) is None
    assert await settings_entries(web_app) == []


@pytest.fixture
def docker(tmp_path: Path, web_app: FastAPI) -> FakeDocker:
    """A usable fake Docker on the application."""
    fake = FakeDocker(tmp_path / "fake-usable")
    fake.mount_socket()
    web_app.state.docker_cli = fake.cli()
    return fake


async def test_the_post_deploy_switch_is_saved_audited_and_reported(
    root: httpx.AsyncClient, web_app: FastAPI, docker: FakeDocker
) -> None:
    """With Docker usable the status reads Mounted and Reachable and the switch can be changed."""
    page = (await root.get("/settings/post-deploy")).text
    assert "Post-deploy commands are off" not in page
    assert re.search(r'<tr id="docker-socket">.*?Mounted', page, re.DOTALL)
    assert re.search(r'<tr id="docker-daemon">.*?Reachable', page, re.DOTALL)
    assert re.search(r'<tr id="docker-compose">.*?v2\.40\.0', page, re.DOTALL)
    assert re.search(r'<input id="pd-enabled"[^>]*? checked', page)
    assert not re.search(r'<input id="pd-enabled"[^>]*? disabled(?=[\s>])', page)

    off = await post(root, "/settings/post-deploy", {})
    assert off.status_code == 200
    assert toast_of(off.text) == (
        "Post-deploy settings saved",
        "Deploys skip the stored commands and say why.",
    )
    assert not re.search(r'<input id="pd-enabled"[^>]*? checked', off.text)
    assert (await stored(web_app)).post_deploy.enabled is False
    (entry,) = await settings_entries(web_app)
    assert entry.details == {
        "section": "post_deploy",
        "changes": [{"setting": "post_deploy.enabled", "old": True, "new": False}],
    }
    again = await post(root, "/settings/post-deploy", {})
    assert toast_of(again.text) == ("Nothing to save", "The settings are unchanged.")
    on = await post(root, "/settings/post-deploy", {"enabled": "on"})
    assert toast_of(on.text) is not None and (await stored(web_app)).post_deploy.enabled is True


async def test_the_general_tab_shows_the_defaults(root: httpx.AsyncClient) -> None:
    """Zone, date format, deployment defaults and the diagnostics panel are all drawn."""
    page = (await root.get("/settings/general")).text
    assert selected_of(page, "tz") == "UTC"
    assert selected_of(page, "df") == "iso"
    assert [selected_of(page, name) for name in ("d1", "d2", "d3")] == ["block", "halt", "create"]
    assert [value_of(page, name) for name in ("d4", "d5", "d6")] == ["0600", "", ""]
    assert value_of(page, "ar") == "90"
    assert '<option value="Europe/Lisbon"' in page and '<option value="UTC" selected' in page
    for label in ("2026-10-17 09:42", "17/10/2026 09:42", "10/17/2026 9:42 AM"):
        assert label in page
    assert 'data-fragment="settings-diagnostics"' in page and "Read-only. Set at startup." in page


async def test_saving_the_general_tab_stores_audits_and_fills_the_deployment_form(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Changed defaults are stored, audited and pre-fill the new deployment form only."""
    form = {
        "time_zone": "Europe/Lisbon",
        "date_format": "day_first",
        "audit_retention_days": "30",
        "dist_missing_policy": "warn",
        "drift_policy": "backup_overwrite",
        "missing_target_policy": "halt",
        "file_mode": "640",
        "file_owner": "www-data",
        "file_group": "deploy",
    }
    response = await post(root, "/settings/general", form)
    assert response.status_code == 200
    assert toast_of(response.text) == ("General settings saved", "They apply now.")
    assert value_of(response.text, "d4") == "0640"
    general = (await stored(web_app)).general
    assert (general.time_zone, general.date_format, general.file_mode) == (
        "Europe/Lisbon",
        "day_first",
        "0640",
    )
    assert general.audit_retention_days == 30 and value_of(response.text, "ar") == "30"
    (entry,) = await settings_entries(web_app)
    settings = [change["setting"] for change in changes_of(entry)]
    assert settings == [f"general.{name}" for name in form]

    async with web_app.state.session_factory() as db:
        project = (
            await db.execute(select(Project).where(Project.name == "Fancy Proj. Name"))
        ).scalar_one()
    new_form = (await root.get(f"/projects/{project.id}/deployments/new?source=folder")).text
    assert re.search(r'name="dist_missing_policy" value="warn" checked', new_form)
    assert re.search(r'name="drift_policy" value="backup_overwrite" checked', new_form)
    assert re.search(r'name="missing_target_policy" value="halt" checked', new_form)
    assert value_of(new_form, "mode") == "0640"
    assert value_of(new_form, "owner") == "www-data" and value_of(new_form, "group") == "deploy"


@pytest.mark.parametrize(
    ("field", "bad", "field_id"),
    [
        ("time_zone", "Mars/Base", "tz"),
        ("date_format", "x", "df"),
        ("audit_retention_days", "-1", "ar"),
        ("audit_retention_days", "abc", "ar"),
        ("audit_retention_days", "", "ar"),
        ("audit_retention_days", "36501", "ar"),
        ("dist_missing_policy", "never", "d1"),
        ("drift_policy", "x", "d2"),
        ("missing_target_policy", "x", "d3"),
        ("file_mode", "999", "d4"),
        ("file_mode", "06000", "d4"),
        ("file_owner", "bad name!", "d5"),
        ("file_group", "x" * 65, "d6"),
    ],
)
async def test_a_refused_general_value_shows_its_reason_and_stores_nothing(
    root: httpx.AsyncClient, web_app: FastAPI, field: str, bad: str, field_id: str
) -> None:
    """Each field is checked on the server and a refusal leaves everything as it was."""
    response = await post(root, "/settings/general", {**GENERAL_FORM, field: bad})
    assert response.status_code == 422
    assert error_of(response.text, field_id)
    assert await settings_entries(web_app) == []


async def test_saving_the_same_general_values_says_nothing_changed(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Unchanged values give the notice and no audit entry."""
    response = await post(root, "/settings/general", GENERAL_FORM)
    assert toast_of(response.text) == ("Nothing to save", "The settings are unchanged.")
    assert await settings_entries(web_app) == []


async def test_an_edited_deployment_keeps_its_own_values_when_the_defaults_change(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The defaults pre-fill a new deployment; the edit form shows what the deployment stores."""
    async with web_app.state.session_factory() as db:
        prod = (await db.execute(select(Deployment).where(Deployment.name == "prod"))).scalar_one()
    before = (await root.get(f"/deployments/{prod.id}/edit")).text
    changed = {**GENERAL_FORM, "file_mode": "0644", "drift_policy": "overwrite"}
    await post(root, "/settings/general", changed)
    after = (await root.get(f"/deployments/{prod.id}/edit")).text
    assert value_of(after, "mode") == value_of(before, "mode") != "0644"
    assert re.search(r'name="drift_policy" value="overwrite" checked', after) is None


async def test_the_audit_screens_show_times_in_the_chosen_zone_and_format(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A moment stored in UTC is written in the display zone, with the zone as the title."""
    async with web_app.state.session_factory() as db:
        entry = AuditEntry(
            action="settings.update",
            result=AuditResult.SUCCESS,
            actor_type=AuditActorType.USER,
            actor_name="clock-test",
            created_at=datetime(2026, 7, 1, 23, 30, tzinfo=UTC),
            details={},
        )
        db.add(entry)
        await db.commit()
        entry_id = entry.id
    url = f"/audit?entry={entry_id}"
    assert "2026-07-01 23:30" in (await root.get(url)).text
    await post(
        root,
        "/settings/general",
        {**GENERAL_FORM, "time_zone": "Europe/Lisbon", "date_format": "month_first"},
    )
    page = (await root.get(url)).text
    assert (
        '<time datetime="2026-07-01T23:30:00Z" title="Europe/Lisbon">07/02/2026 12:30 AM</time>'
        in page
    )


async def test_the_diagnostics_show_startup_values_without_secrets(
    root: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel names the version and the resolved address and prints an endpoint's host only."""
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "https://user:pa55word@otel.example.com:4318/v1/traces?key=abc123",
    )
    page = (await root.get("/settings/general")).text
    diagnostics = page[page.index('data-fragment="settings-diagnostics"') :]
    assert re.search(r'id="diag-otel"[^>]*>.*?On', diagnostics, re.DOTALL)
    pattern = r'id="diag-otel-host"[^>]*><span class="font-mono">([^<]+)</span>'
    host = re.search(pattern, diagnostics)
    assert host is not None and host.group(1) == "otel.example.com"
    pattern = r'id="diag-client"[^>]*><span class="font-mono">([^<]+)</span>'
    client = re.search(pattern, diagnostics)
    assert client is not None and client.group(1) == "127.0.0.1"
    assert f"v{VERSION}" in diagnostics
    for secret in ("pa55word", "abc123", "user:", "/v1/traces", "4318"):
        assert secret not in page
