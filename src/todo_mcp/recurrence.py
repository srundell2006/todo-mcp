"""Build and describe Graph patternedRecurrence objects for To Do tasks."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS: list[str] = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
INDEXES = ["first", "second", "third", "fourth", "last"]


class RecurrenceSpec(BaseModel):
    """How a task repeats. Anchored on the task's due date (a due date is required)."""

    frequency: Literal["daily", "weekdays", "weekly", "monthly", "yearly"] = Field(
        description="daily | weekdays (Mon-Fri) | weekly | monthly | yearly"
    )
    interval: int = Field(1, ge=1, le=999, description="Every N days/weeks/months/years. Default 1.")
    days_of_week: list[Weekday] | None = Field(
        None, description="weekly only: which days. Default: the due date's weekday."
    )
    day_of_month: int | None = Field(
        None, ge=1, le=31, description="monthly/yearly: day of the month. Default: the due date's day."
    )
    week_of_month: Literal["first", "second", "third", "fourth", "last"] | None = Field(
        None,
        description="monthly/yearly by weekday instead of date, e.g. 'second' + weekday 'tuesday' = "
        "the second Tuesday. Uses the due date's weekday unless 'weekday' is given.",
    )
    weekday: Weekday | None = Field(None, description="Weekday for week_of_month patterns.")
    month: int | None = Field(None, ge=1, le=12, description="yearly: month number. Default: the due date's month.")
    end_date: date | None = Field(None, description="Last date the task may repeat (YYYY-MM-DD). Omit for no end.")
    occurrences: int | None = Field(None, ge=1, le=999, description="Stop after this many occurrences.")

    @model_validator(mode="after")
    def _check(self) -> RecurrenceSpec:
        if self.end_date and self.occurrences:
            raise ValueError("Use either end_date or occurrences, not both.")
        if self.week_of_month and self.frequency not in ("monthly", "yearly"):
            raise ValueError("week_of_month only applies to monthly or yearly recurrence.")
        if self.days_of_week and self.frequency != "weekly":
            raise ValueError("days_of_week only applies to weekly recurrence.")
        return self


def build(spec: RecurrenceSpec, start: date) -> dict:
    """Graph patternedRecurrence anchored on `start` (the task's due date)."""
    pattern: dict = {"interval": spec.interval}
    if spec.frequency == "daily":
        pattern["type"] = "daily"
    elif spec.frequency == "weekdays":
        pattern.update(type="weekly", interval=1, daysOfWeek=WEEKDAYS[:5], firstDayOfWeek="sunday")
    elif spec.frequency == "weekly":
        days = spec.days_of_week or [WEEKDAYS[start.weekday()]]
        pattern.update(type="weekly", daysOfWeek=list(dict.fromkeys(days)), firstDayOfWeek="sunday")
    elif spec.frequency in ("monthly", "yearly"):
        yearly = spec.frequency == "yearly"
        if spec.week_of_month:
            pattern.update(
                type="relativeYearly" if yearly else "relativeMonthly",
                daysOfWeek=[spec.weekday or WEEKDAYS[start.weekday()]],
                index=spec.week_of_month,
            )
        else:
            pattern.update(
                type="absoluteYearly" if yearly else "absoluteMonthly",
                dayOfMonth=spec.day_of_month or start.day,
            )
        if yearly:
            pattern["month"] = spec.month or start.month
    rng: dict = {"startDate": start.isoformat()}
    if spec.end_date:
        if spec.end_date < start:
            raise ValueError("end_date is before the task's due date.")
        rng.update(type="endDate", endDate=spec.end_date.isoformat())
    elif spec.occurrences:
        rng.update(type="numbered", numberOfOccurrences=spec.occurrences)
    else:
        rng["type"] = "noEnd"
    return {"pattern": pattern, "range": rng}


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe(rec: dict | None) -> str | None:
    if not rec or not rec.get("pattern"):
        return None
    p = rec["pattern"]
    r = rec.get("range") or {}
    n = p.get("interval") or 1
    kind = p.get("type", "")
    days = [d.capitalize() for d in (p.get("daysOfWeek") or [])]

    def every(unit: str) -> str:
        return f"every {unit}" if n == 1 else f"every {n} {unit}s"

    if kind == "daily":
        text = every("day")
    elif kind == "weekly":
        if n == 1 and [d.lower() for d in days] == WEEKDAYS[:5]:
            text = "every weekday"
        else:
            text = f"{every('week')} on {', '.join(days)}" if days else every("week")
    elif kind == "absoluteMonthly":
        text = f"{every('month')} on the {_ordinal(p.get('dayOfMonth') or 1)}"
    elif kind == "relativeMonthly":
        text = f"{every('month')} on the {p.get('index', 'first')} {', '.join(days)}"
    elif kind == "absoluteYearly":
        text = f"{every('year')} on {_MONTHS[(p.get('month') or 1) - 1]} {p.get('dayOfMonth') or 1}"
    elif kind == "relativeYearly":
        text = (f"{every('year')} on the {p.get('index', 'first')} {', '.join(days)} of "
                f"{_MONTHS[(p.get('month') or 1) - 1]}")
    else:
        text = f"repeats ({kind})"
    if r.get("type") == "endDate" and r.get("endDate"):
        text += f", until {r['endDate']}"
    elif r.get("type") == "numbered" and r.get("numberOfOccurrences"):
        text += f", {r['numberOfOccurrences']} times"
    return text
