"""Public clinic information for the website (services, prices, opening hours)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from .. import presenters, timeutil
from ..config import get_settings
from ..models import Clinician, Service
from ..services.clinic_hours import is_open, opening_hours
from .deps import DbSession, Phone

router = APIRouter(prefix="/api", tags=["clinic"])


@router.get("/clinic")
async def clinic_info(db: DbSession, telephony: Phone) -> dict[str, Any]:
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
    open_now = is_open(hours)
    # Whether the SOS button can ring our doctors (if not, it only offers 999).
    emergency_line = telephony.enabled and any(c.sos_available and (c.mobile_phone or "").strip() for c in clinicians)
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
        "emergency_line": emergency_line,
        "services": [presenters.service_payload(s) for s in services],
        "clinicians": [presenters.clinician_payload(c) for c in clinicians],
    }
