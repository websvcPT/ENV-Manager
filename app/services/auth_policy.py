"""Authentication thresholds: session timeouts, lockout limits and password rules."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class AuthPolicy:
    """Tunable authentication limits. The defaults apply until settings override them."""

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
