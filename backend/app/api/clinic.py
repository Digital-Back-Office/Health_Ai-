"""Public clinic information for the website (services, prices, opening hours)."""

from __future__ import annotations

import calendar as pycalendar
from datetime import time
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from .. import presenters, timeutil
from ..config import get_settings
from ..models import Clinician, Service
from .deps import DbSession

router = APIRouter(prefix="/api", tags=["clinic"])


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


@router.get("/clinic")
async def clinic_info(db: DbSession) -> dict[str, Any]:
    settings = get_settings()
    services = (
        await db.execute(select(Service).where(Service.is_active.is_(True)).order_by(Service.display_order, Service.name))
    ).scalars().all()
    clinicians = list(
        (await db.execute(select(Clinician).where(Clinician.is_active.is_(True)).order_by(Clinician.full_name))).scalars().all()
    )
    hours = opening_hours(clinicians)
    now_local = timeutil.to_clinic(timeutil.now_utc())
    today = hours[now_local.weekday()]
    open_now = bool(today["open"]) and today["open"] <= f"{now_local:%H:%M}" < today["close"]
    return {
        "name": settings.clinic_name,
        "assistant_name": settings.assistant_name,
        "address": settings.clinic_address,
        "phone": settings.clinic_phone,
        "email": settings.clinic_email,
        "timezone": settings.clinic_timezone,
        "open_now": open_now,
        "today": today,
        "opening_hours": hours,
        "services": [presenters.service_payload(s) for s in services],
        "clinicians": [presenters.clinician_payload(c) for c in clinicians],
    }
