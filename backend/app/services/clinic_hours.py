"""Clinic opening hours, derived from clinicians' working hours."""

from __future__ import annotations

import calendar as pycalendar
from datetime import datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import timeutil
from ..models import Clinician


def opening_hours(clinicians: list[Clinician]) -> list[dict[str, Any]]:
    """Clinic opening hours = earliest start / latest finish of anyone working that day."""
    days: list[dict[str, Any]] = []
    for weekday in range(7):
        windows = [w for c in clinicians for w in c.working_hours if w.weekday == weekday]
        if windows:
            opens: time = min(w.start_time for w in windows)
            closes: time = max(w.end_time for w in windows)
            days.append({"weekday": weekday, "day": pycalendar.day_name[weekday], "open": f"{opens:%H:%M}", "close": f"{closes:%H:%M}"})
        else:
            days.append({"weekday": weekday, "day": pycalendar.day_name[weekday], "open": None, "close": None})
    return days


def is_open(hours: list[dict[str, Any]], now: datetime | None = None) -> bool:
    local = timeutil.to_clinic(now or timeutil.now_utc())
    today = hours[local.weekday()]
    return bool(today["open"]) and today["open"] <= f"{local:%H:%M}" < today["close"]


async def clinic_open_now(db: AsyncSession, now: datetime | None = None) -> bool:
    clinicians = list((await db.execute(select(Clinician).where(Clinician.is_active.is_(True)))).scalars())
    return is_open(opening_hours(clinicians), now)
