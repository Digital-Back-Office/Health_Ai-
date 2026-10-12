"""SMS confirmations for bookings, reschedules and cancellations (sent after the change commits)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import timeutil
from ..config import get_settings
from ..models import Appointment, AppointmentEvent
from .telephony import Telephony, TelephonyError

log = logging.getLogger(__name__)

SMS_KINDS = ("booked", "rescheduled", "cancelled")


def _when(dt: datetime) -> str:
    local = timeutil.to_clinic(dt)
    return f"{local:%a} {local.day} {local:%b}, {timeutil.speak_time(dt)}"


def appointment_sms(kind: str, appointment: Appointment) -> str:
    """Short, single-purpose texts: no clinical detail beyond the service name."""
    settings = get_settings()
    clinic = settings.clinic_name
    what = f"{appointment.service.name} with {appointment.clinician.full_name}"
    when = _when(appointment.starts_at)
    ref = appointment.reference
    if kind == "booked":
        text = f"{clinic}: your {what} is booked for {when}. Ref {ref}. To change it, call {settings.clinic_phone}."
    elif kind == "rescheduled":
        text = f"{clinic}: your {what} has moved to {when}. Ref {ref}. Questions? Call {settings.clinic_phone}."
    elif kind == "cancelled":
        text = f"{clinic}: your {what} on {when} has been cancelled. Ref {ref}. To rebook, call {settings.clinic_phone}."
    else:
        raise ValueError(f"unknown SMS kind {kind!r}")
    return text


async def send_appointment_sms(
    sessionmaker: async_sessionmaker[AsyncSession], telephony: Telephony, appointment_id: uuid.UUID, kind: str
) -> str:
    """Best effort: records sms_sent / sms_failed in the appointment's audit trail. Returns the outcome."""
    settings = get_settings()
    if not settings.sms_enabled or not telephony.enabled:
        return "skipped"
    async with sessionmaker() as db:
        appointment = await db.get(Appointment, appointment_id)
        if appointment is None:
            return "missing"
        body = appointment_sms(kind, appointment)
        try:
            sid = await telephony.send_sms(appointment.patient.phone, body)
            db.add(AppointmentEvent(appointment_id=appointment.id, action="sms_sent", details={"kind": kind, "sid": sid}))
            outcome = "sent"
        except TelephonyError as exc:
            log.warning("SMS for appointment %s failed: %s", appointment.reference, exc)
            db.add(AppointmentEvent(appointment_id=appointment.id, action="sms_failed", details={"kind": kind, "error": str(exc)[:300]}))
            outcome = "failed"
        await db.commit()
        return outcome
