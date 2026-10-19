"""Time zone and date format for the times people read on the audit screens.

Everything is stored and compared in UTC. ``DisplayTime`` only changes how a moment is written
for a person: converted to the configured zone and laid out in one of the date formats. The CSV
export of the audit log keeps UTC, since a file is read by programs and by people in other zones.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from starlette.requests import HTTPConnection

UTC_NAME = "UTC"
DEFAULT_DATE_FORMAT = "iso"
HIDDEN_ZONE_PREFIXES = ("posix/", "right/", "Etc/", "SystemV/")

# Key, label (an example moment, as the settings screen draws it) and the pieces of the layout.
DATE_FORMATS: dict[str, tuple[str, str, str]] = {
    "iso": ("2026-10-17 09:42", "%Y-%m-%d", "%H:%M"),
    "day_first": ("17/10/2026 09:42", "%d/%m/%Y", "%H:%M"),
    "month_first": ("10/17/2026 9:42 AM", "%m/%d/%Y", "%I:%M %p"),
}


@lru_cache(maxsize=64)
def load_zone(name: str) -> tzinfo | None:
    """The zone with this name, None when this system does not know it. UTC always exists."""
    if name == UTC_NAME:
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError, OSError:
        return None


@lru_cache(maxsize=1)
def zone_names() -> tuple[str, ...]:
    """The zones a person may choose: UTC first, then every region zone this system knows."""
    names = sorted(
        name
        for name in available_timezones()
        if name != UTC_NAME and not name.startswith(HIDDEN_ZONE_PREFIXES)
    )
    return (UTC_NAME, *names)


@dataclass(frozen=True)
class DisplayTime:
    """How times are written for people: a zone name and a date format key."""

    zone_name: str = UTC_NAME
    date_format: str = DEFAULT_DATE_FORMAT

    @property
    def zone(self) -> tzinfo:
        """The zone, UTC when the stored name is unknown on this system."""
        return load_zone(self.zone_name) or UTC

    @property
    def label(self) -> str:
        """The zone name as shown next to times, UTC when the stored name is unknown."""
        return self.zone_name if load_zone(self.zone_name) is not None else UTC_NAME

    def local(self, moment: datetime) -> datetime:
        """The moment in the display zone. A naive value is read as UTC."""
        aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
        return aware.astimezone(self.zone)

    def text(self, moment: datetime) -> str:
        """The moment as date and time in the display zone and format."""
        _label, day, clock = DATE_FORMATS.get(self.date_format, DATE_FORMATS[DEFAULT_DATE_FORMAT])
        local = self.local(moment)
        return f"{local.strftime(day)} {_clock(local, clock)}"

    def clock(self, moment: datetime) -> str:
        """The time of day in the display zone and format, without the date."""
        _label, _day, clock = DATE_FORMATS.get(self.date_format, DATE_FORMATS[DEFAULT_DATE_FORMAT])
        return _clock(self.local(moment), clock)


def _clock(moment: datetime, pattern: str) -> str:
    """The time of day, with no leading zero on a twelve hour clock."""
    text = moment.strftime(pattern)
    if "%I" in pattern:
        return text.lstrip("0")
    return text


def display_of(connection: HTTPConnection) -> DisplayTime:
    """The display settings loaded for this request, UTC and ISO when none were loaded."""
    found: DisplayTime | None = getattr(connection.state, "display", None)
    return found if found is not None else DisplayTime()
