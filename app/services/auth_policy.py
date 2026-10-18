"""Authentication thresholds: session timeouts, lockout limits and password rules."""

from dataclasses import dataclass
from datetime import timedelta

from app.services.app_settings import SecuritySettings


@dataclass(frozen=True)
class AuthPolicy:
    """Authentication limits. The defaults are the ones of an installation with no settings."""

    idle_timeout: timedelta = timedelta(minutes=30)
    absolute_timeout: timedelta = timedelta(hours=12)
    touch_interval: timedelta = timedelta(minutes=1)
    min_password_length: int = 12
    user_max_failures: int = 5
    ip_max_failures: int = 20
    api_ip_max_failures: int = 10
    failure_window: timedelta = timedelta(minutes=15)
    lockout_duration: timedelta = timedelta(minutes=15)
    pending_login_timeout: timedelta = timedelta(minutes=5)


def policy_from_settings(
    security: SecuritySettings, *, ip_max_failures_override: int | None = None
) -> AuthPolicy:
    """The limits in force: the stored security settings, read at request time.

    The failure window, the touch interval and the pending sign-in lifetime are not settings and
    keep their code values. ``ip_max_failures_override`` is the development and testing variable
    ``ENVMANAGER_LOGIN_IP_MAX_FAILURES``: when set it replaces the stored sign-in limit per
    address, so browser specs that share one client address never lock themselves out.
    """
    return AuthPolicy(
        idle_timeout=timedelta(minutes=security.idle_timeout_minutes),
        absolute_timeout=timedelta(hours=security.absolute_timeout_hours),
        min_password_length=security.min_password_length,
        user_max_failures=security.user_max_failures,
        ip_max_failures=(
            ip_max_failures_override
            if ip_max_failures_override is not None
            else security.ip_max_failures
        ),
        api_ip_max_failures=security.api_ip_max_failures,
        lockout_duration=timedelta(minutes=security.lockout_minutes),
    )
