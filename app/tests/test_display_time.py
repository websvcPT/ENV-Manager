"""Time zone and date format for the audit screens, and the audit date filters that follow them."""

import csv
import io
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.core.display_time import (
    DATE_FORMATS,
    DEFAULT_DATE_FORMAT,
    DisplayTime,
    duration_text,
    load_zone,
    zone_names,
)
from app.models import AuditActorType, AuditEntry, AuditResult
from app.seeds import SeedData
from app.services.app_settings import GeneralSettings
from app.services.audit_export import export_chunks, record_export
from app.services.audit_filters import AuditFilters
from app.services.audit_log import list_page
from app.services.permissions import compute_visibility, load_user_scope

SUMMER = datetime(2026, 7, 1, 23, 30, tzinfo=UTC)
WINTER = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)


def test_the_default_writes_the_utc_moment_exactly_as_the_screens_always_did() -> None:
    """The default zone and format are byte identical to the earlier strftime pattern."""
    moment = datetime(2026, 10, 17, 9, 42, 31, tzinfo=UTC)
    assert DisplayTime().text(moment) == moment.strftime("%Y-%m-%d %H:%M") == "2026-10-17 09:42"
    assert DisplayTime().label == "UTC"
    assert GeneralSettings().date_format == DEFAULT_DATE_FORMAT
    assert GeneralSettings().time_zone == DisplayTime().zone_name


@pytest.mark.parametrize(
    ("date_format", "expected"),
    [
        ("iso", "2026-07-02 00:30"),
        ("day_first", "02/07/2026 00:30"),
        ("month_first", "07/02/2026 12:30 AM"),
    ],
)
def test_each_format_writes_the_same_moment_its_own_way(date_format: str, expected: str) -> None:
    """The zone moves the moment across midnight and the format lays it out."""
    assert DisplayTime("Europe/Lisbon", date_format).text(SUMMER) == expected


def test_the_twelve_hour_clock_has_no_leading_zero_and_marks_noon_and_midnight() -> None:
    """9:42 AM, 12:05 PM, 12:00 AM: the hour is never zero padded."""
    display = DisplayTime("UTC", "month_first")
    assert display.text(datetime(2026, 10, 17, 9, 42, tzinfo=UTC)) == "10/17/2026 9:42 AM"
    assert display.text(datetime(2026, 10, 17, 12, 5, tzinfo=UTC)) == "10/17/2026 12:05 PM"
    assert display.text(datetime(2026, 10, 17, 0, 0, tzinfo=UTC)) == "10/17/2026 12:00 AM"
    assert display.text(datetime(2026, 10, 17, 21, 7, tzinfo=UTC)) == "10/17/2026 9:07 PM"


def test_daylight_saving_follows_the_zone_rules() -> None:
    """Lisbon is on UTC in winter and an hour ahead in summer; New York is behind."""
    assert DisplayTime("Europe/Lisbon").text(WINTER) == "2026-01-01 23:30"
    assert DisplayTime("Europe/Lisbon").text(SUMMER) == "2026-07-02 00:30"
    assert DisplayTime("America/New_York").text(SUMMER) == "2026-07-01 19:30"
    assert DisplayTime("America/New_York").text(WINTER) == "2026-01-01 18:30"


def test_an_unknown_stored_zone_falls_back_to_utc_and_says_so() -> None:
    """A zone name this system lacks never fails a page; the label tells what is used."""
    display = DisplayTime("Mars/Olympus")
    assert display.text(SUMMER) == "2026-07-01 23:30" and display.label == "UTC"
    assert load_zone("Mars/Olympus") is None and load_zone("UTC") is UTC


def test_a_naive_moment_is_read_as_utc() -> None:
    """Values from outside the database that carry no zone are taken as UTC, never as local."""
    assert DisplayTime("Europe/Lisbon").text(datetime(2026, 7, 1, 23, 30)) == "2026-07-02 00:30"


def test_the_zone_list_starts_with_utc_and_holds_region_zones_only() -> None:
    """UTC first, then regions; the legacy and technical names are not offered."""
    names = zone_names()
    assert names[0] == "UTC" and len(names) > 300 and names.count("UTC") == 1
    assert {"Europe/Lisbon", "America/New_York", "Asia/Tokyo"} <= set(names)
    assert not [name for name in names if name.startswith(("posix/", "right/", "Etc/"))]
    assert list(names[1:]) == sorted(names[1:])


def test_every_format_has_an_example_the_screen_can_show() -> None:
    """The choices carry the example text of the settings screen."""
    assert {key: label for key, (label, _day, _clock) in DATE_FORMATS.items()} == {
        "iso": "2026-10-17 09:42",
        "day_first": "17/10/2026 09:42",
        "month_first": "10/17/2026 9:42 AM",
    }


async def add_entries(engine: AsyncEngine, *moments: datetime) -> None:
    """Add one audit entry at each moment, with an action of its own."""
    async with create_session_factory(engine)() as db:
        for number, moment in enumerate(moments):
            db.add(
                AuditEntry(
                    action=f"zone.test{number}",
                    result=AuditResult.SUCCESS,
                    actor_type=AuditActorType.USER,
                    actor_name="tester",
                    created_at=moment,
                    details={},
                )
            )
        await db.commit()


async def actions_in(engine: AsyncEngine, seed_data: SeedData, filters: AuditFilters) -> list[str]:
    """The zone test actions the superadmin's log lists under a filter, oldest first."""
    async with create_session_factory(engine)() as db:
        scope = await load_user_scope(db, seed_data.users.superadmin)
        page = await list_page(db, await compute_visibility(db, scope), filters)
    return sorted(row.action for row in page.rows if row.action.startswith("zone.test"))


async def test_the_date_filters_are_days_of_the_display_zone(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """An entry at 00:30 Lisbon time on July 2 is on July 2 in Lisbon and on July 1 in UTC."""
    late_july_first = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)
    early_july_second = datetime(2026, 7, 1, 23, 30, tzinfo=UTC)
    await add_entries(seeded_database, late_july_first, early_july_second)
    day = date(2026, 7, 2)
    lisbon = DisplayTime("Europe/Lisbon").zone
    assert await actions_in(
        seeded_database, seed_data, AuditFilters(date_from=day, date_to=day, zone=lisbon)
    ) == ["zone.test1"]
    assert (
        await actions_in(seeded_database, seed_data, AuditFilters(date_from=day, date_to=day)) == []
    )
    july_first = date(2026, 7, 1)
    assert await actions_in(
        seeded_database, seed_data, AuditFilters(date_from=july_first, date_to=july_first)
    ) == ["zone.test0", "zone.test1"]
    in_lisbon = AuditFilters(date_from=july_first, date_to=july_first, zone=lisbon)
    assert await actions_in(seeded_database, seed_data, in_lisbon) == ["zone.test0"]


async def test_a_zone_behind_utc_moves_the_boundaries_the_other_way(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """In New York an evening entry belongs to the day before its UTC date."""
    await add_entries(seeded_database, datetime(2026, 7, 2, 1, 30, tzinfo=UTC))
    new_york = DisplayTime("America/New_York").zone
    on = date(2026, 7, 1)
    assert await actions_in(
        seeded_database, seed_data, AuditFilters(date_from=on, date_to=on, zone=new_york)
    ) == ["zone.test0"]
    assert (
        await actions_in(seeded_database, seed_data, AuditFilters(date_from=on, date_to=on)) == []
    )


def test_the_zone_is_not_part_of_the_filter_address() -> None:
    """The URL carries the days as typed; the zone only decides what they mean."""
    lisbon = timezone(timedelta(hours=1))
    parsed = AuditFilters.parse({"from": "2026-07-02", "to": "2026-07-02"}, lisbon)
    assert parsed.params() == {"from": "2026-07-02", "to": "2026-07-02"}
    assert parsed.zone == lisbon and AuditFilters.parse({}).zone is UTC


async def test_the_csv_export_keeps_utc_whatever_the_display_zone(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """A file is read by programs and by people elsewhere: its times stay UTC with a Z."""
    await add_entries(seeded_database, SUMMER)
    lisbon = DisplayTime("Europe/Lisbon").zone
    filters = AuditFilters(action="zone.test0", zone=lisbon)
    async with create_session_factory(seeded_database)() as db:
        scope = await load_user_scope(db, seed_data.users.superadmin)
        visibility = await compute_visibility(db, scope)
        plan = await record_export(db, visibility, seed_data.users.superadmin, filters, None)
        await db.commit()
    factory = create_session_factory(seeded_database)
    chunks = [chunk async for chunk in export_chunks(factory, visibility, filters, plan.up_to_id)]
    rows = list(csv.reader(io.StringIO("".join(chunks))))
    assert rows[0][1] == "time_utc"
    assert rows[1][1] == "2026-07-01T23:30:00Z"


@pytest.mark.parametrize(
    ("span", "text"),
    [
        (timedelta(seconds=0), "0 seconds"),
        (timedelta(seconds=1), "1 second"),
        (timedelta(seconds=59, milliseconds=900), "59 seconds"),
        (timedelta(seconds=60), "1 minute"),
        (timedelta(minutes=5, seconds=59), "5 minutes"),
        (timedelta(minutes=60), "1 hour"),
        (timedelta(hours=2, minutes=59), "2 hours"),
        (timedelta(hours=24), "1 day"),
        (timedelta(hours=47, minutes=59), "1 day"),
        (timedelta(days=3, hours=5), "3 days"),
        (timedelta(days=400), "400 days"),
        (timedelta(seconds=-5), "0 seconds"),
    ],
)
def test_a_duration_is_written_in_its_largest_whole_unit(span: timedelta, text: str) -> None:
    assert duration_text(span) == text
