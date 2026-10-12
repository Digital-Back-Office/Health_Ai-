"""Clinic-local time helpers.

All instants are stored and compared in UTC. Clinic rules (working hours,
"tomorrow", "morning") are applied in the clinic's timezone, so BST/GMT
changes are handled by zoneinfo rather than by hand.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .config import get_settings


@lru_cache
def clinic_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().clinic_timezone)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_clinic(dt: datetime) -> datetime:
    return dt.astimezone(clinic_tz())


def clinic_today(now: datetime | None = None) -> date:
    return to_clinic(now or now_utc()).date()


def local_datetime(day: date, at: time) -> datetime:
    """Clinic-local wall-clock time as an aware datetime."""
    return datetime.combine(day, at, tzinfo=clinic_tz())


def local_day_bounds_utc(first_day: date, last_day: date) -> tuple[datetime, datetime]:
    """UTC instants covering clinic-local midnight of first_day to midnight after last_day."""
    start = local_datetime(first_day, time.min).astimezone(UTC)
    end = local_datetime(last_day + timedelta(days=1), time.min).astimezone(UTC)
    return start, end


def parse_instant(value: str | datetime) -> datetime:
    """Parse an ISO 8601 date-time. Naive values are taken as clinic-local time."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=clinic_tz())
    return dt.astimezone(UTC)


def iso_local(dt: datetime) -> str:
    """ISO 8601 in clinic-local time with offset, e.g. 2026-10-14T14:30:00+01:00."""
    return to_clinic(dt).isoformat(timespec="minutes")


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def speak_date(day: date, today: date | None = None) -> str:
    """'Tuesday 14th October' (year added only when it isn't this year)."""
    text = f"{day:%A} {ordinal(day.day)} {day:%B}"
    if today is not None and day.year != today.year:
        text += f" {day.year}"
    return text


def speak_time(dt: datetime) -> str:
    """'9am', '2:30pm', '12pm' in clinic-local time."""
    local = to_clinic(dt)
    hour12 = local.hour % 12 or 12
    meridiem = "am" if local.hour < 12 else "pm"
    if local.minute == 0:
        return f"{hour12}{meridiem}"
    return f"{hour12}:{local.minute:02d}{meridiem}"


def speak_datetime(dt: datetime, today: date | None = None) -> str:
    local = to_clinic(dt)
    return f"{speak_date(local.date(), today)} at {speak_time(local)}"


def format_price(pence: int) -> str:
    pounds, rem = divmod(pence, 100)
    return f"£{pounds}" if rem == 0 else f"£{pounds}.{rem:02d}"
