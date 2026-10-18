"""The security settings: the form rules, the policy built from them and open sessions."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import UserSession
from app.models.types import utc_now
from app.services.app_settings import SecuritySettings, load_stored_settings
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy, policy_from_settings
from app.services.security_settings import (
    BOUNDS,
    IDLE_LONGER_MESSAGE,
    SecurityForm,
    SecurityFormError,
    save_security,
    validate_security,
)
from app.services.sessions import create_session, resolve_session
from app.tests.auth_helpers import make_user


def form(**overrides: str) -> SecurityForm:
    """A form with the default values, some replaced."""
    values = {**SecurityForm.from_settings(SecuritySettings()).__dict__, **overrides}
    return SecurityForm(**values)


def refused(**overrides: str) -> dict[str, str]:
    """The messages by field of a form that is refused."""
    with pytest.raises(SecurityFormError) as error:
        validate_security(form(**overrides), stored=SecuritySettings(), address_limit_fixed=False)
    return error.value.errors


def test_the_defaults_in_code_make_the_default_policy() -> None:
    """A database with no rows gives the policy an installation had before settings existed."""
    assert policy_from_settings(SecuritySettings()) == AuthPolicy()


def test_every_stored_value_reaches_the_policy() -> None:
    """Minutes, hours and counts map onto the policy; the code constants keep their values."""
    stored = SecuritySettings(
        idle_timeout_minutes=45,
        absolute_timeout_hours=8,
        user_max_failures=3,
        ip_max_failures=40,
        api_ip_max_failures=7,
        lockout_minutes=20,
        min_password_length=16,
    )
    policy = policy_from_settings(stored)
    assert policy.idle_timeout == timedelta(minutes=45)
    assert policy.absolute_timeout == timedelta(hours=8)
    assert (policy.user_max_failures, policy.ip_max_failures, policy.api_ip_max_failures) == (
        3,
        40,
        7,
    )
    assert policy.lockout_duration == timedelta(minutes=20)
    assert policy.min_password_length == 16
    assert policy.failure_window == AuthPolicy().failure_window
    assert policy.touch_interval == AuthPolicy().touch_interval
    assert policy.pending_login_timeout == AuthPolicy().pending_login_timeout


def test_the_startup_override_beats_the_stored_address_limit_only() -> None:
    """ENVMANAGER_LOGIN_IP_MAX_FAILURES replaces the sign-in limit per address and nothing else."""
    stored = SecuritySettings(ip_max_failures=40, api_ip_max_failures=7)
    policy = policy_from_settings(stored, ip_max_failures_override=900)
    assert policy.ip_max_failures == 900
    assert policy.api_ip_max_failures == 7
    assert policy_from_settings(stored, ip_max_failures_override=None).ip_max_failures == 40


def test_a_valid_form_is_read_as_numbers() -> None:
    """Surrounding spaces are ignored and every value becomes a whole number."""
    clean = validate_security(
        form(idle_timeout_minutes=" 60 ", min_password_length="20"),
        stored=SecuritySettings(),
        address_limit_fixed=False,
    )
    assert clean == replace(SecuritySettings(), idle_timeout_minutes=60, min_password_length=20)


@pytest.mark.parametrize("name", list(BOUNDS))
def test_each_setting_accepts_its_limits_and_refuses_one_step_beyond(name: str) -> None:
    """The lowest and highest values pass; the values just outside, text and blanks do not."""
    low, high = BOUNDS[name].low, BOUNDS[name].high
    base = {"idle_timeout_minutes": "5", "absolute_timeout_hours": "168"}
    for accepted in (str(low), str(high)):
        validate_security(
            form(**{**base, name: accepted}), stored=SecuritySettings(), address_limit_fixed=False
        )
    for bad in (str(low - 1), str(high + 1), "", " ", "1e3", "-1", "+5", "0x10", "٣"):
        assert name in refused(**{**base, name: bad}), (name, bad)
    assert refused(**{name: "x"})[name] == BOUNDS[name].message


def test_the_idle_timeout_may_not_exceed_the_absolute_timeout() -> None:
    """Idle is in minutes and absolute in hours, so 121 minutes does not fit in two hours."""
    assert refused(idle_timeout_minutes="121", absolute_timeout_hours="2") == {
        "idle_timeout_minutes": IDLE_LONGER_MESSAGE
    }
    validate_security(
        form(idle_timeout_minutes="120", absolute_timeout_hours="2"),
        stored=SecuritySettings(),
        address_limit_fixed=False,
    )


def test_a_fixed_address_limit_keeps_the_stored_value_whatever_was_posted() -> None:
    """With the startup override in force the field is ignored, even when it is nonsense."""
    stored = replace(SecuritySettings(), ip_max_failures=33)
    clean = validate_security(
        form(ip_max_failures="not a number"), stored=stored, address_limit_fixed=True
    )
    assert clean.ip_max_failures == 33


async def make_session(
    engine: AsyncEngine, started_minutes_ago: int, policy: AuthPolicy
) -> tuple[str, int]:
    """A session that started some minutes ago under a policy; its token and the user id."""
    async with create_session_factory(engine)() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        started = utc_now() - timedelta(minutes=started_minutes_ago)
        token = await create_session(db, user, policy, now=started)
        await db.commit()
        return token, user.id


async def test_a_shorter_absolute_timeout_ends_sessions_that_are_already_open(
    engine: AsyncEngine,
) -> None:
    """Saving 1 hour ends a session that started two hours ago, at its next request."""
    wide = AuthPolicy(idle_timeout=timedelta(days=2), absolute_timeout=timedelta(hours=12))
    token, _ = await make_session(engine, 120, wide)
    factory = create_session_factory(engine)
    async with factory() as db:
        assert await resolve_session(db, token, wide) is not None
        await save_security(
            db, replace(SecuritySettings(), absolute_timeout_hours=1), actor=AuditActor.cli()
        )
        stored = await load_stored_settings(db)
        policy = policy_from_settings(stored.security)
        assert policy.absolute_timeout == timedelta(hours=1)
    async with factory() as db:
        assert await resolve_session(db, token, wide) is None
        assert (await db.execute(select(UserSession))).first() is None


async def test_a_longer_absolute_timeout_keeps_open_sessions_alive(engine: AsyncEngine) -> None:
    """A session that would have ended in half an hour runs on once the limit is raised."""
    narrow = AuthPolicy(idle_timeout=timedelta(days=2), absolute_timeout=timedelta(hours=1))
    token, _ = await make_session(engine, 30, narrow)
    factory = create_session_factory(engine)
    async with factory() as db:
        await save_security(
            db,
            replace(SecuritySettings(), idle_timeout_minutes=60, absolute_timeout_hours=24),
            actor=AuditActor.cli(),
        )
    later = utc_now() + timedelta(hours=2)
    wide = AuthPolicy(idle_timeout=timedelta(days=2), absolute_timeout=timedelta(hours=24))
    async with factory() as db:
        assert await resolve_session(db, token, wide, now=later) is not None


async def test_other_security_changes_leave_session_expiry_alone(engine: AsyncEngine) -> None:
    """Only a changed absolute timeout recomputes the expiry of open sessions."""
    narrow = AuthPolicy(idle_timeout=timedelta(days=2), absolute_timeout=timedelta(hours=1))
    await make_session(engine, 10, narrow)
    factory = create_session_factory(engine)
    async with factory() as db:
        before = (await db.execute(select(UserSession.expires_at))).scalar_one()
        await save_security(
            db, replace(SecuritySettings(), user_max_failures=9), actor=AuditActor.cli()
        )
    async with factory() as db:
        assert (await db.execute(select(UserSession.expires_at))).scalar_one() == before


async def test_a_session_that_already_ran_out_stays_ended_when_the_limit_is_raised(
    engine: AsyncEngine,
) -> None:
    """A longer limit never brings back a session whose row was only waiting to be purged."""
    narrow = AuthPolicy(idle_timeout=timedelta(days=2), absolute_timeout=timedelta(hours=1))
    token, _ = await make_session(engine, 90, narrow)
    factory = create_session_factory(engine)
    async with factory() as db:
        await save_security(
            db,
            replace(SecuritySettings(), idle_timeout_minutes=1440, absolute_timeout_hours=24),
            actor=AuditActor.cli(),
        )
    wide = AuthPolicy(idle_timeout=timedelta(days=2), absolute_timeout=timedelta(hours=24))
    async with factory() as db:
        assert await resolve_session(db, token, wide) is None
