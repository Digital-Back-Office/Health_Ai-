"""Reminder calls: the evening before an appointment, Sophie phones the patient.

She checks she's speaking to the patient, reminds them of the appointment and lets
them confirm, reschedule or cancel (the same tools as any other conversation).
Voicemail gets a short message with no appointment details.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import timeutil
from ..config import get_settings
from ..models import Appointment, AppointmentStatus, VoiceSession
from . import sessions as session_service
from .elevenlabs import ElevenLabsClient, ElevenLabsError, load_state, session_token_var
from .telephony import TelephonyError, to_e164

log = logging.getLogger(__name__)

CALL_WINDOW_END_HOUR = 20  # don't ring patients after 8pm
RECENT_BOOKING = timedelta(hours=2)  # just booked? no need for a reminder


def in_call_window(now: datetime | None = None) -> bool:
    local = timeutil.to_clinic(now or timeutil.now_utc())
    return get_settings().reminder_call_hour <= local.hour < CALL_WINDOW_END_HOUR


async def due_reminders(db: AsyncSession, now: datetime | None = None) -> list[Appointment]:
    """Tomorrow's booked appointments that haven't had a reminder call yet."""
    now = now or timeutil.now_utc()
    if not in_call_window(now):
        return []
    tomorrow = timeutil.clinic_today(now) + timedelta(days=1)
    start, end = timeutil.local_day_bounds_utc(tomorrow, tomorrow)
    rows = await db.execute(
        select(Appointment)
        .where(
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.reminder_status.is_(None),
            Appointment.starts_at >= start,
            Appointment.starts_at < end,
            Appointment.created_at < now - RECENT_BOOKING,
        )
        .order_by(Appointment.starts_at)
    )
    return list(rows.scalars())


def appointment_summary(appointment: Appointment, now: datetime | None = None) -> str:
    today = timeutil.clinic_today(now)
    day = timeutil.to_clinic(appointment.starts_at).date()
    relative = "tomorrow, " if day == today + timedelta(days=1) else ""
    return (
        f"{appointment.service.name} with {appointment.clinician.full_name} {relative}"
        f"{timeutil.speak_datetime(appointment.starts_at, today)}"
    )


def reminder_first_message(appointment: Appointment) -> str:
    settings = get_settings()
    return (
        f"Hello, this is {settings.assistant_name}, the AI assistant at {settings.clinic_name}. "
        f"I'm calling for {appointment.patient.first_name} about an appointment. Is that you?"
    )


def voicemail_message() -> str:
    settings = get_settings()
    return (
        f"Hello, this is {settings.assistant_name} from {settings.clinic_name}, calling about an upcoming appointment. "
        f"Please call us back on {settings.clinic_phone}. Thank you."
    )


async def place_reminder_call(
    sessionmaker: async_sessionmaker[AsyncSession], elevenlabs: ElevenLabsClient, appointment_id: uuid.UUID
) -> dict[str, Any]:
    """Create a pre-verified session for the patient and ask ElevenLabs to ring them."""
    state = load_state()
    phone_number_id = state.get("phone_number_id")
    if not phone_number_id:
        return {"ok": False, "error": "phone_not_set_up", "message": "Run setup-agent with Twilio configured first."}

    async with sessionmaker() as db:
        appointment = await db.get(Appointment, appointment_id)
        if appointment is None or appointment.status != AppointmentStatus.BOOKED or appointment.starts_at <= timeutil.now_utc():
            return {"ok": False, "error": "not_remindable", "message": "Only upcoming booked appointments get reminders."}
        try:
            to_number = to_e164(appointment.patient.phone)
        except TelephonyError:
            appointment.reminder_status = "skipped"
            await db.commit()
            return {"ok": False, "error": "bad_number", "message": "The patient's phone number isn't valid."}

        # We ring the number on the patient's record, so the session starts verified for them.
        session, token = await session_service.create_session(db, mode="voice", channel="phone")
        session.purpose = "reminder"
        session.appointment_ref = appointment.id
        session.patient_id = appointment.patient_id
        session.patient = appointment.patient
        session.verified_at = timeutil.now_utc()
        session.caller_number = appointment.patient.phone
        session.expires_at = timeutil.now_utc() + timedelta(minutes=30)
        appointment.reminder_status = "calling"
        appointment.reminder_called_at = timeutil.now_utc()
        summary = appointment_summary(appointment)
        first_message = reminder_first_message(appointment)
        session_id = session.id
        await db.commit()

    try:
        result = await elevenlabs.outbound_call(
            to_number=to_number,
            phone_number_id=phone_number_id,
            dynamic_variables={
                session_token_var(): token,
                "channel": "phone",
                "caller_known": "yes",
                "call_purpose": "reminder",
                "appointment_summary": summary,
            },
            first_message=first_message,
        )
    except ElevenLabsError as exc:
        async with sessionmaker() as db:
            appointment = await db.get(Appointment, appointment_id)
            if appointment is not None:
                appointment.reminder_status = "failed"
            await db.commit()
        return {"ok": False, "error": "call_failed", "message": str(exc)}

    async with sessionmaker() as db:
        appointment = await db.get(Appointment, appointment_id)
        session = await db.get(VoiceSession, session_id)
        if appointment is not None:
            appointment.reminder_conversation_id = result.get("conversation_id")
        if session is not None:
            session.conversation_id = result.get("conversation_id")
            session.call_sid = result.get("callSid")
        await db.commit()
    return {"ok": True, "conversation_id": result.get("conversation_id")}


async def run_due_reminders(sessionmaker: async_sessionmaker[AsyncSession], elevenlabs: ElevenLabsClient) -> int:
    """Ring each patient with a reminder due, one at a time (gentle on concurrency limits)."""
    settings = get_settings()
    async with sessionmaker() as db:
        due = [a.id for a in await due_reminders(db)]
    for index, appointment_id in enumerate(due):
        if index:
            await asyncio.sleep(settings.reminder_call_spacing_seconds)
        if not in_call_window():
            break
        result = await place_reminder_call(sessionmaker, elevenlabs, appointment_id)
        log.info("Reminder call for %s: %s", appointment_id, result.get("error") or "placed")
    return len(due)
