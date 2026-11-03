from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from todo_mcp import recurrence as rec
from todo_mcp import timeutil as tu

CHI = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)  # Tue 2026-11-03 14:00 CST


@pytest.mark.parametrize(
    "stored, expected",
    [
        ({"dateTime": "2026-11-05T06:00:00.0000000", "timeZone": "UTC"}, date(2026, 11, 5)),   # CST midnight
        ({"dateTime": "2026-07-04T05:00:00.0000000", "timeZone": "UTC"}, date(2026, 7, 4)),    # CDT midnight
        ({"dateTime": "2026-11-05T00:00:00.0000000", "timeZone": "UTC"}, date(2026, 11, 5)),   # UTC midnight
        ({"dateTime": "2026-11-05T00:00:00.0000000", "timeZone": "Central Standard Time"}, date(2026, 11, 5)),
        ({"dateTime": "2026-11-05T00:00:00", "timeZone": "America/Chicago"}, date(2026, 11, 5)),
        (None, None),
    ],
)
def test_due_date_round_trip(stored, expected):
    assert tu.parse_graph_due_date(stored, CHI) == expected


def test_due_written_as_local_midnight():
    assert tu.due_to_graph(date(2026, 11, 5), CHI) == {
        "dateTime": "2026-11-05T00:00:00.0000000", "timeZone": "Central Standard Time"}
    assert tu.parse_graph_due_date(tu.due_to_graph(date(2026, 3, 8), CHI), CHI) == date(2026, 3, 8)


def test_reminder_parsing_and_conversion():
    dt = tu.parse_datetime_input("2026-11-05T09:00", CHI, NOW)
    assert dt.utcoffset().total_seconds() == -6 * 3600
    assert tu.to_graph(dt, CHI) == {"dateTime": "2026-11-05T09:00:00.0000000", "timeZone": "Central Standard Time"}
    utc = tu.parse_datetime_input("2026-11-05T15:00Z", CHI, NOW)
    assert tu.to_graph(utc, CHI)["dateTime"] == "2026-11-05T09:00:00.0000000"
    assert tu.parse_datetime_input("tomorrow 08:30", CHI, NOW) == datetime(2026, 11, 4, 8, 30, tzinfo=CHI)
    back = tu.parse_graph_datetime({"dateTime": "2026-11-05T15:00:00.0000000", "timeZone": "UTC"}, CHI)
    assert tu.iso_local(back) == "2026-11-05T09:00-06:00"
    summer = tu.parse_graph_datetime({"dateTime": "2026-07-01T14:00:00.0000000", "timeZone": "UTC"}, CHI)
    assert tu.iso_local(summer) == "2026-07-01T09:00-05:00"


def test_relative_dates():
    assert tu.parse_date_input("today", CHI, NOW) == date(2026, 11, 3)
    assert tu.parse_date_input("tomorrow", CHI, NOW) == date(2026, 11, 4)
    assert tu.parse_date_input("friday", CHI, NOW) == date(2026, 11, 6)
    assert tu.parse_date_input("tuesday", CHI, NOW) == date(2026, 11, 10)  # next one, not today
    late = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc)  # 22:30 on the 3rd in Chicago
    assert tu.parse_date_input("today", CHI, late) == date(2026, 11, 3)
    with pytest.raises(tu.DateInputError):
        tu.parse_date_input("someday", CHI, NOW)


def test_recurrence_presets():
    d = date(2026, 11, 5)  # Thursday
    assert rec.build(rec.RecurrenceSpec(frequency="daily"), d) == {
        "pattern": {"type": "daily", "interval": 1}, "range": {"type": "noEnd", "startDate": "2026-11-05"}}
    wk = rec.build(rec.RecurrenceSpec(frequency="weekdays"), d)
    assert wk["pattern"]["daysOfWeek"] == ["monday", "tuesday", "wednesday", "thursday", "friday"]
    weekly = rec.build(rec.RecurrenceSpec(frequency="weekly", interval=2), d)
    assert weekly["pattern"] == {"type": "weekly", "interval": 2, "daysOfWeek": ["thursday"], "firstDayOfWeek": "sunday"}
    monthly = rec.build(rec.RecurrenceSpec(frequency="monthly", occurrences=6), d)
    assert monthly["pattern"] == {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 5}
    assert monthly["range"] == {"type": "numbered", "startDate": "2026-11-05", "numberOfOccurrences": 6}
    rel = rec.build(rec.RecurrenceSpec(frequency="monthly", week_of_month="second", weekday="tuesday"), d)
    assert rel["pattern"] == {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["tuesday"], "index": "second"}
    yearly = rec.build(rec.RecurrenceSpec(frequency="yearly", end_date=date(2030, 1, 1)), d)
    assert yearly["pattern"] == {"type": "absoluteYearly", "interval": 1, "dayOfMonth": 5, "month": 11}
    assert yearly["range"]["type"] == "endDate"


def test_recurrence_validation():
    with pytest.raises(ValueError):
        rec.RecurrenceSpec(frequency="daily", end_date=date(2027, 1, 1), occurrences=3)
    with pytest.raises(ValueError):
        rec.RecurrenceSpec(frequency="daily", week_of_month="first")
    with pytest.raises(ValueError):
        rec.build(rec.RecurrenceSpec(frequency="daily", end_date=date(2020, 1, 1)), date(2026, 1, 1))


def test_recurrence_descriptions():
    d = date(2026, 11, 5)
    assert rec.describe(rec.build(rec.RecurrenceSpec(frequency="weekdays"), d)) == "every weekday"
    assert rec.describe(rec.build(rec.RecurrenceSpec(frequency="weekly", interval=2,
                                                     days_of_week=["monday", "friday"]), d)) == \
        "every 2 weeks on Monday, Friday"
    assert rec.describe(rec.build(rec.RecurrenceSpec(frequency="monthly", occurrences=3), d)) == \
        "every month on the 5th, 3 times"
    assert rec.describe(None) is None
