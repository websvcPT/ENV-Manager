"""Helpers shared by the Settings tests: a second browser, form values and stored settings."""

import html
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AppSetting, AuditEntry
from app.models.app_setting import KEY_CHECK_SETTING
from app.services.app_settings import StoredSettings, load_stored_settings, save_section
from app.services.audit import AuditActor
from app.tests.conftest import sign_in

TABS = (
    "paths",
    "smtp",
    "git",
    "backup",
    "security",
    "sso",
    "post-deploy",
    "registries",
    "notifications",
    "general",
)
SETTINGS_ACTIONS = (
    "settings.update",
    "allowed_path.add",
    "allowed_path.remove",
    "registry.add",
    "registry.update",
    "registry.remove",
    "registry.test",
)
NON_SUPERADMINS = (
    "admin",
    "project_admin",
    "deployment_admin",
    "viewer",
    "env_scoped_admin",
    "env_scoped_viewer",
    "ungranted_admin",
)


@asynccontextmanager
async def browser_as(web_app: FastAPI, user: str) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in browser of a seeded user, on the same application."""
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Accept": "text/html"}
    ) as http:
        await sign_in(http, user)
        yield http


def value_of(page: str, field_id: str) -> str:
    """The value of the text input with this id."""
    found = re.search(rf'<input id="{field_id}"[^>]*? value="([^"]*)"', page)
    assert found is not None, field_id
    return html.unescape(found.group(1))


def selected_of(page: str, field_id: str) -> str:
    """The value of the selected option of the select with this id."""
    select_markup = re.search(rf'<select id="{field_id}".*?</select>', page, re.DOTALL)
    assert select_markup is not None, field_id
    found = re.search(r'<option value="([^"]*)" selected', select_markup.group(0))
    assert found is not None, field_id
    return html.unescape(found.group(1))


def error_of(page: str, field_id: str) -> str | None:
    """The refusal under the field with this id, None when there is none."""
    pattern = rf'<p id="{field_id}-e" role="alert"[^>]*>(?:<svg.*?</svg>)?([^<]+)</p>'
    found = re.search(pattern, page)
    return html.unescape(found.group(1)) if found else None


def toast_of(page: str) -> tuple[str, str] | None:
    """The title and message of the toast in a response, None when there is none."""
    found = re.search(
        r'<div role="(?:status|alert)" data-fragment="toast".*?'
        r'<p class="text-\[13px\] font-bold">([^<]+)</p>'
        r'<p class="text-\[13px\] text-ink2">([^<]+)</p>',
        page,
        re.DOTALL,
    )
    return (html.unescape(found.group(1)), html.unescape(found.group(2))) if found else None


async def stored(web_app: FastAPI) -> StoredSettings:
    """The settings as stored in the application's database now."""
    async with web_app.state.session_factory() as db:
        return await load_stored_settings(db)


async def settings_entries(web_app: FastAPI) -> list[AuditEntry]:
    """Every audit entry that records a settings change, oldest first."""
    async with web_app.state.session_factory() as db:
        return list(
            (
                await db.execute(
                    select(AuditEntry)
                    .where(AuditEntry.action.in_(SETTINGS_ACTIONS))
                    .order_by(AuditEntry.id)
                )
            ).scalars()
        )


def changes_of(entry: AuditEntry) -> list[dict[str, object]]:
    """The changes listed in the details of a settings audit entry."""
    changes = entry.details["changes"]
    assert isinstance(changes, list)
    return changes


async def stored_rows(web_app: FastAPI) -> dict[str, str]:
    """The settings rows in the database, without the key check record."""
    async with web_app.state.session_factory() as db:
        rows = (await db.execute(select(AppSetting.key, AppSetting.value))).all()
    return {key: value for key, value in rows if key != KEY_CHECK_SETTING}


async def set_log_masking(web_app: FastAPI, masked: bool) -> None:
    """Store whether the container logs are masked, as the Security tab would."""
    async with web_app.state.session_factory() as db:
        current = (await load_stored_settings(db)).security
        await save_section(
            db,
            "security",
            replace(current, mask_container_logs=masked),
            actor=AuditActor.system("test"),
        )
        await db.commit()
