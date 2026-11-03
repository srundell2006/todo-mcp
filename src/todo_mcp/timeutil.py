"""Time zone handling for Graph's dateTimeTimeZone values.

Graph returns To Do dates as {"dateTime": "2026-11-05T06:00:00.0000000", "timeZone": "UTC"}
(seven fractional digits, a Windows or IANA zone name). Everything this server shows or
accepts is in the configured local zone (America/Chicago by default).

Due dates are date-only in To Do. They are written as local midnight and read back by
converting to the local zone and taking the date, with one exception: a value that is
exactly midnight in the zone it was stored in is taken as that calendar date as-is, so
a due date that another client wrote as "midnight UTC" doesn't shift by a day.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Windows zone names Graph may return, mapped to IANA (subset of CLDR windowsZones).
WINDOWS_TO_IANA: dict[str, str] = {
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "tzone://Microsoft/Utc": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indianapolis",
    "Central Standard Time": "America/Chicago",
    "Central America Standard Time": "America/Guatemala",
    "Canada Central Standard Time": "America/Regina",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "India Standard Time": "Asia/Calcutta",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
}
IANA_TO_WINDOWS: dict[str, str] = {
    "America/Chicago": "Central Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Phoenix": "US Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Anchorage": "Alaskan Standard Time",
    "Pacific/Honolulu": "Hawaiian Standard Time",
    "Europe/London": "GMT Standard Time",
    "UTC": "UTC",
}

_FRACTION = re.compile(r"(\.\d{1,6})\d*")


class DateInputError(ValueError):
    pass


def zone(name: str | None) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    iana = WINDOWS_TO_IANA.get(name, name)
    try:
        return ZoneInfo(iana)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def windows_name(tz: ZoneInfo) -> str | None:
    return IANA_TO_WINDOWS.get(tz.key)


def _parse_naive(value: str) -> datetime:
    value = value.strip().replace(" ", "T", 1)
    if value.endswith("Z"):
        value = value[:-1]
    value = _FRACTION.sub(lambda m: m.group(1), value)
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=None) if dt.tzinfo is None else dt


def parse_graph_datetime(obj: dict | None, local: ZoneInfo) -> datetime | None:
    """Graph dateTimeTimeZone -> aware datetime in the local zone."""
    if not obj or not obj.get("dateTime"):
        return None
    dt = _parse_naive(obj["dateTime"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone(obj.get("timeZone")))
    return dt.astimezone(local)


def parse_graph_due_date(obj: dict | None, local: ZoneInfo) -> date | None:
    """Graph dueDateTime -> the calendar date To Do shows."""
    if not obj or not obj.get("dateTime"):
        return None
    dt = _parse_naive(obj["dateTime"])
    if dt.tzinfo is None:
        stored = dt.replace(tzinfo=zone(obj.get("timeZone")))
    else:
        stored = dt
    if stored.time() == time(0, 0):
        return stored.date()
    return stored.astimezone(local).date()


def parse_offset_timestamp(value: str | None, local: ZoneInfo) -> datetime | None:
    """createdDateTime / lastModifiedDateTime (DateTimeOffset, UTC) -> local."""
    if not value:
        return None
    dt = _parse_naive(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local)


def to_graph(dt: datetime, local: ZoneInfo) -> dict:
    """Aware datetime -> Graph dateTimeTimeZone, expressed in the local zone."""
    local_dt = dt.astimezone(local)
    name = windows_name(local)
    if name is None:
        local_dt = dt.astimezone(timezone.utc)
        name = "UTC"
    return {"dateTime": local_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": name}


def due_to_graph(d: date, local: ZoneInfo) -> dict:
    return to_graph(datetime.combine(d, time(0, 0), tzinfo=local), local)


def today(local: ZoneInfo, now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(local).date()


_RELATIVE = {"today": 0, "tomorrow": 1, "yesterday": -1}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_date_input(value: str, local: ZoneInfo, now: datetime | None = None) -> date:
    """'2026-11-05', 'today', 'tomorrow', 'yesterday', or a weekday name (next occurrence)."""
    raw = value.strip().lower()
    base = today(local, now)
    if raw in _RELATIVE:
        return base + timedelta(days=_RELATIVE[raw])
    name = raw.removeprefix("next ").strip()
    if name in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(name) - base.weekday()) % 7 or 7
        return base + timedelta(days=ahead)
    try:
        if "t" in raw or " " in raw:
            return _parse_naive(raw).date()
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise DateInputError(
            f"Couldn't read the date {value!r}. Use YYYY-MM-DD, 'today', 'tomorrow', or a weekday name."
        ) from exc


def parse_datetime_input(value: str, local: ZoneInfo, now: datetime | None = None) -> datetime:
    """ISO date-time (with or without offset; no offset means local time), or
    'today 09:00' / 'tomorrow 17:30' / '<weekday> 08:00'."""
    raw = value.strip()
    parts = raw.split()
    if len(parts) == 2 and re.fullmatch(r"\d{1,2}:\d{2}", parts[1]):
        d = parse_date_input(parts[0], local, now)
        hh, mm = (int(x) for x in parts[1].split(":"))
        return datetime.combine(d, time(hh, mm), tzinfo=local)
    try:
        text = raw.replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DateInputError(
            f"Couldn't read the date-time {value!r}. Use e.g. 2026-11-05T09:00 (local time), "
            "2026-11-05T15:00Z, or 'tomorrow 09:00'."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local)
    return dt


def iso_local(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="minutes") if dt else None
