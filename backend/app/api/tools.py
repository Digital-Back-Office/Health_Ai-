"""Tool webhooks called by the ElevenLabs agent during a conversation.

Conventions (the agent's system prompt relies on them):
* Always HTTP 200 with {"ok": true|false, ...}. Business errors carry an
  "error" code and a "message" telling the agent what to do next.
* Dates/times come back both as ISO values (to pass back into other tools)
  and "spoken" text (to read out).
* The patient is identified by the session token header - never by an id
  chosen by the LLM - so one conversation can only ever act for the patient
  it verified.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import presenters, timeutil
from ..config import get_settings
from ..models import CallbackRequest, Service, VoiceSession
from ..services import booking, emergency, handoff, patients
from ..services import sessions as session_service
from ..services.availability import find_slots, get_service
from ..services.booking import BookingError, sync_appointment_calendar
from ..services.calendar import CalendarError
from ..services.events import publish
from ..services.notifications import send_appointment_sms
from .deps import Calendar, DbSession, Phone, SessionFactory, require_tool_secret
from .parsing import blank_to_none, parse_flexible_date

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["agent-tools"], dependencies=[Depends(require_tool_secret)])

SessionTokenHeader = Annotated[str | None, Header(alias="X-Session-Token")]


# --- request models -----------------------------------------------------------


class ToolRequest(BaseModel):
    # Fallback transport for the session token if it can't be sent as a header.
    session_token: str | None = None


class VerifyPatientIn(ToolRequest):
    last_name: str = Field(min_length=1, max_length=80)
    date_of_birth: date
    # Optional on phone calls: the caller's own number is used when it's left empty.
    phone: str | None = Field(default=None, max_length=24)

    _dob = field_validator("date_of_birth", mode="before")(parse_flexible_date)
    _phone = field_validator("phone", mode="before")(blank_to_none)


class RegisterPatientIn(ToolRequest):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    date_of_birth: date
    phone: str = Field(min_length=6, max_length=24)
    postcode: str | None = Field(default=None, max_length=10)
    email: str | None = Field(default=None, max_length=254)

    _dob = field_validator("date_of_birth", mode="before")(parse_flexible_date)
    _optional = field_validator("email", "postcode", mode="before")(blank_to_none)


class CheckAvailabilityIn(ToolRequest):
    service_code: str
    clinician_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    part_of_day: Literal["any", "morning", "afternoon", "evening"] = "any"

    _clinician = field_validator("clinician_id", mode="before")(blank_to_none)
    _dates = field_validator("date_from", "date_to", mode="before")(parse_flexible_date)

    @field_validator("part_of_day", mode="before")
    @classmethod
    def _part_of_day(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            return "any"
        value = value.strip().lower()
        return value if value in {"any", "morning", "afternoon", "evening"} else "any"


class BookAppointmentIn(ToolRequest):
    service_code: str
    clinician_id: int
    start_time: str


class RescheduleAppointmentIn(ToolRequest):
    appointment_reference: str | None = None
    new_start_time: str
    clinician_id: int | None = None

    _blank = field_validator("appointment_reference", "clinician_id", mode="before")(blank_to_none)


class CancelAppointmentIn(ToolRequest):
    appointment_reference: str | None = None
    reason: str | None = Field(default=None, max_length=500)

    _blank = field_validator("appointment_reference", "reason", mode="before")(blank_to_none)


class RequestCallbackIn(ToolRequest):
    reason: str = Field(min_length=2, max_length=1000)
    phone: str | None = None
    name: str | None = Field(default=None, max_length=160)
    preferred_time: str | None = Field(default=None, max_length=120)

    _blank = field_validator("phone", "name", "preferred_time", mode="before")(blank_to_none)


# --- helpers ---------------------------------------------------------------------


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, **extra}


SESSION_INVALID = _error(
    "session_invalid",
    "This conversation's secure session has expired. Apologise and ask the patient to refresh the page "
    "and start a new conversation.",
)


async def _session(db: AsyncSession, header_token: str | None, body: ToolRequest) -> VoiceSession | None:
    session = await session_service.resolve_session(db, header_token or body.session_token, for_update=True)
    if session is not None:
        session_service.touch(session)
    return session


async def _announce(factory: SessionFactory, session: VoiceSession | None, tool: str) -> None:
    """Tell the UI a tool is running (committed immediately, outside the main transaction)."""
    if session is None:
        return
    try:
        async with factory() as db:
            await publish(db, session.id, "tool_started", {"tool": tool})
            await db.commit()
    except Exception:  # UI nicety only - never fail the tool because of it
        log.exception("Could not publish tool_started event")


def _booking_error_payload(exc: BookingError) -> dict[str, Any]:
    today = timeutil.clinic_today()
    extra: dict[str, Any] = {}
    if exc.alternatives:
        extra["alternatives"] = [presenters.slot_payload(s, today) for s in exc.alternatives]
    if exc.appointments:
        extra["appointments"] = [presenters.appointment_payload(a, today) for a in exc.appointments]
    return _error(exc.code, exc.message, **extra)


CALENDAR_UNAVAILABLE = _error(
    "calendar_unavailable",
    "The clinic diary can't be reached right now. Apologise, and offer a callback from the team instead.",
)


def _sms_will_be_sent(telephony: Phone) -> bool:
    return telephony.enabled and get_settings().sms_enabled


def _schedule_sms(background: BackgroundTasks, factory: SessionFactory, telephony: Phone, appointment_id: uuid.UUID, kind: str) -> None:
    if _sms_will_be_sent(telephony):
        background.add_task(send_appointment_sms, factory, telephony, appointment_id, kind)


def _confirmation_note(session: VoiceSession, telephony: Phone) -> str:
    notes = []
    if session.channel == "web":
        notes.append("A confirmation is shown on the patient's screen.")
    if _sms_will_be_sent(telephony):
        notes.append("They'll also get a text message confirming it.")
    return " ".join(notes)


def _schedule_calendar_sync(
    background: BackgroundTasks, factory: SessionFactory, calendar: Calendar, appointment_id: uuid.UUID
) -> None:
    if calendar.enabled:
        background.add_task(sync_appointment_calendar, factory, calendar, appointment_id)


# --- tools -------------------------------------------------------------------------


@router.post("/verify_patient")
async def verify_patient(
    body: VerifyPatientIn, db: DbSession, x_session_token: SessionTokenHeader = None
) -> dict[str, Any]:
    session = await _session(db, x_session_token, body)
    if session is None:
        return SESSION_INVALID
    settings = get_settings()
    phone = body.phone or session.caller_number
    if not phone:
        await db.commit()
        return _error("phone_required", "Ask for the phone number they registered with, then try again.")
    try:
        result = await patients.verify_patient(
            db, session, last_name=body.last_name, date_of_birth=body.date_of_birth, phone=phone
        )
    except patients.PatientInputError as exc:
        await db.commit()
        return _error("invalid_input", f"{exc}. Ask the patient to repeat it.")

    if result.verified and result.patient is not None:
        today = timeutil.clinic_today()
        upcoming = await booking.upcoming_appointments(db, result.patient.id)
        appointments = [presenters.appointment_payload(a, today) for a in upcoming]
        await publish(
            db,
            session.id,
            "identity_verified",
            {**presenters.patient_greeting_payload(result.patient), "appointments": appointments},
        )
        await db.commit()
        return {
            "ok": True,
            "verified": True,
            "first_name": result.patient.first_name,
            "is_minor": patients.is_minor(result.patient),
            "upcoming_appointments": appointments,
            "message": "Identity confirmed. Thank the patient by first name and continue with their request.",
        }

    if result.locked:
        await publish(db, session.id, "identity_locked", {})
        await db.commit()
        return _error(
            "verification_locked",
            "Too many unsuccessful identity checks. Do not try again in this conversation. "
            f"Offer a callback (request_callback) or ask them to call the clinic on {settings.clinic_phone}.",
        )
    await db.commit()
    return _error(
        "no_match",
        "Those details don't match a patient record. Ask the patient to spell their surname and to repeat "
        "their date of birth and the phone number they registered with. If they've never been to the clinic "
        "before, register them instead.",
        attempts_remaining=result.attempts_remaining,
    )


@router.post("/register_patient")
async def register_patient(
    body: RegisterPatientIn, db: DbSession, x_session_token: SessionTokenHeader = None
) -> dict[str, Any]:
    session = await _session(db, x_session_token, body)
    if session is None:
        return SESSION_INVALID
    try:
        result = await patients.register_patient(
            db,
            session,
            first_name=body.first_name,
            last_name=body.last_name,
            date_of_birth=body.date_of_birth,
            postcode=body.postcode,
            phone=body.phone,
            email=body.email,
        )
    except patients.PatientInputError as exc:
        await db.commit()
        return _error("invalid_input", f"{exc}. Ask the patient to repeat or correct it.")

    today = timeutil.clinic_today()
    upcoming = await booking.upcoming_appointments(db, result.patient.id) if result.already_registered else []
    appointments = [presenters.appointment_payload(a, today) for a in upcoming]
    await publish(
        db,
        session.id,
        "identity_verified",
        {**presenters.patient_greeting_payload(result.patient), "appointments": appointments, "new_patient": not result.already_registered},
    )
    await db.commit()
    return {
        "ok": True,
        "registered": not result.already_registered,
        "already_registered": result.already_registered,
        "first_name": result.patient.first_name,
        "is_minor": patients.is_minor(result.patient),
        "upcoming_appointments": appointments,
        "message": (
            "This patient already has a record and is now verified - welcome them back."
            if result.already_registered
            else "New patient registered and verified. Continue with their request."
        ),
    }


@router.post("/check_availability")
async def check_availability(
    body: CheckAvailabilityIn,
    db: DbSession,
    calendar: Calendar,
    factory: SessionFactory,
    x_session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    settings = get_settings()
    session = await _session(db, x_session_token, body)
    await _announce(factory, session, "check_availability")

    service = await get_service(db, body.service_code)
    if service is None:
        codes = (await db.execute(select(Service.code).where(Service.is_active.is_(True)))).scalars().all()
        await db.commit()
        return _error("unknown_service", "Unknown service_code. Use one of the valid codes.", valid_codes=list(codes))

    today = timeutil.clinic_today()
    date_from = max(body.date_from or today, today)
    date_to = body.date_to or date_from + timedelta(days=settings.default_search_days - 1)
    if date_to < date_from:
        date_to = date_from
    date_to = min(date_to, date_from + timedelta(days=settings.max_search_days - 1))

    try:
        slots = await find_slots(
            db,
            calendar,
            service=service,
            date_from=date_from,
            date_to=date_to,
            clinician_id=body.clinician_id,
            part_of_day=body.part_of_day,
            limit=settings.max_slots_returned,
            max_per_day=settings.max_slots_per_day,
        )
        next_available = []
        if not slots:
            next_available = await find_slots(
                db,
                calendar,
                service=service,
                date_from=date_to + timedelta(days=1),
                date_to=today + timedelta(days=settings.booking_horizon_days),
                clinician_id=body.clinician_id,
                part_of_day=body.part_of_day,
                limit=3,
                max_per_day=2,
            )
    except CalendarError:
        log.exception("Calendar unavailable during availability check")
        await db.commit()
        return CALENDAR_UNAVAILABLE

    slot_items = [presenters.slot_payload(s, today) for s in slots]
    next_items = [presenters.slot_payload(s, today) for s in next_available]
    if session is not None:
        await publish(
            db,
            session.id,
            "slots_offered",
            {"service": presenters.service_payload(service), "slots": slot_items, "next_available": next_items},
        )
    await db.commit()

    if slot_items:
        message = (
            "Offer two or three of these options naturally (day, date, time and clinician). When booking, pass "
            "start_time and clinician_id exactly as given. The options are also shown on the patient's screen."
        )
    elif next_items:
        message = "Nothing is free in that range. Offer the earliest alternatives from next_available."
    else:
        message = "There's no availability for this service in the coming weeks. Apologise and offer a callback."
    return {
        "ok": True,
        "service": presenters.service_payload(service),
        "searched": {
            "from": timeutil.speak_date(date_from, today),
            "to": timeutil.speak_date(date_to, today),
            "part_of_day": body.part_of_day,
        },
        "slots": slot_items,
        "next_available": next_items,
        "message": message,
    }


@router.post("/book_appointment")
async def book_appointment(
    body: BookAppointmentIn,
    db: DbSession,
    calendar: Calendar,
    factory: SessionFactory,
    background: BackgroundTasks,
    telephony: Phone,
    x_session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    session = await _session(db, x_session_token, body)
    if session is None:
        return SESSION_INVALID
    await _announce(factory, session, "book_appointment")
    try:
        starts_at = timeutil.parse_instant(body.start_time)
    except ValueError:
        await db.commit()
        return _error("invalid_start_time", "start_time must be copied exactly from check_availability results.")

    try:
        appointment = await booking.book_appointment(
            db,
            calendar,
            session=session,
            service_code=body.service_code,
            clinician_id=body.clinician_id,
            starts_at=starts_at,
        )
    except BookingError as exc:
        payload = _booking_error_payload(exc)
        if exc.alternatives:
            await publish(db, session.id, "slots_offered", {"slots": payload["alternatives"], "next_available": [], "replaced": True})
        await db.commit()
        return payload
    except CalendarError:
        log.exception("Calendar unavailable while booking")
        await db.commit()
        return CALENDAR_UNAVAILABLE

    today = timeutil.clinic_today()
    appointment_data = presenters.appointment_payload(appointment, today)
    await publish(db, session.id, "appointment_booked", {"appointment": appointment_data})
    await db.commit()
    _schedule_calendar_sync(background, factory, calendar, appointment.id)
    _schedule_sms(background, factory, telephony, appointment.id, "booked")

    message = (
        "Booked. Confirm the day, date, time and clinician, then give the booking reference character by "
        f"character. Mention any preparation advice. {_confirmation_note(session, telephony)}"
    ).strip()
    if patients.is_minor(appointment.patient):
        message += " The patient is under 16, so remind them a parent or guardian must attend."
    return {"ok": True, "appointment": appointment_data, "message": message}


@router.post("/get_my_appointments")
async def get_my_appointments(
    db: DbSession,
    body: Annotated[ToolRequest | None, Body()] = None,
    x_session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    session = await _session(db, x_session_token, body or ToolRequest())
    if session is None:
        return SESSION_INVALID
    try:
        patient = booking.require_verified_patient(session)
    except BookingError as exc:
        await db.commit()
        return _booking_error_payload(exc)
    today = timeutil.clinic_today()
    items = [presenters.appointment_payload(a, today) for a in await booking.upcoming_appointments(db, patient.id)]
    await publish(db, session.id, "appointments_listed", {"appointments": items})
    await db.commit()
    return {
        "ok": True,
        "appointments": items,
        "message": "Summarise these for the patient." if items else "The patient has no upcoming appointments.",
    }


@router.post("/reschedule_appointment")
async def reschedule_appointment(
    body: RescheduleAppointmentIn,
    db: DbSession,
    calendar: Calendar,
    factory: SessionFactory,
    background: BackgroundTasks,
    telephony: Phone,
    x_session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    session = await _session(db, x_session_token, body)
    if session is None:
        return SESSION_INVALID
    await _announce(factory, session, "reschedule_appointment")
    try:
        new_start = timeutil.parse_instant(body.new_start_time)
    except ValueError:
        await db.commit()
        return _error("invalid_start_time", "new_start_time must be copied exactly from check_availability results.")
    try:
        result = await booking.reschedule_appointment(
            db,
            calendar,
            session=session,
            reference=body.appointment_reference,
            new_starts_at=new_start,
            clinician_id=body.clinician_id,
        )
    except BookingError as exc:
        payload = _booking_error_payload(exc)
        if exc.alternatives:
            await publish(db, session.id, "slots_offered", {"slots": payload["alternatives"], "next_available": [], "replaced": True})
        await db.commit()
        return payload
    except CalendarError:
        log.exception("Calendar unavailable while rescheduling")
        await db.commit()
        return CALENDAR_UNAVAILABLE

    today = timeutil.clinic_today()
    appointment_data = presenters.appointment_payload(result.appointment, today)
    previous = {
        "spoken": timeutil.speak_datetime(result.previous_starts_at, today),
        "start_time": timeutil.iso_local(result.previous_starts_at),
        "clinician": result.previous_clinician,
    }
    await publish(db, session.id, "appointment_rescheduled", {"appointment": appointment_data, "previous": previous})
    await db.commit()
    _schedule_calendar_sync(background, factory, calendar, result.appointment.id)
    _schedule_sms(background, factory, telephony, result.appointment.id, "rescheduled")
    return {
        "ok": True,
        "appointment": appointment_data,
        "previous": previous,
        "message": (
            "Rescheduled. Confirm the new day, date, time and clinician. The booking reference is unchanged. "
            f"{_confirmation_note(session, telephony)}"
        ).strip(),
    }


@router.post("/cancel_appointment")
async def cancel_appointment(
    body: CancelAppointmentIn,
    db: DbSession,
    calendar: Calendar,
    factory: SessionFactory,
    background: BackgroundTasks,
    telephony: Phone,
    x_session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    settings = get_settings()
    session = await _session(db, x_session_token, body)
    if session is None:
        return SESSION_INVALID
    await _announce(factory, session, "cancel_appointment")
    try:
        result = await booking.cancel_appointment(
            db, calendar, session=session, reference=body.appointment_reference, reason=body.reason
        )
    except BookingError as exc:
        await db.commit()
        return _booking_error_payload(exc)

    today = timeutil.clinic_today()
    appointment_data = presenters.appointment_payload(result.appointment, today)
    await publish(
        db, session.id, "appointment_cancelled", {"appointment": appointment_data, "late_cancellation": result.late_cancellation}
    )
    await db.commit()
    _schedule_calendar_sync(background, factory, calendar, result.appointment.id)
    _schedule_sms(background, factory, telephony, result.appointment.id, "cancelled")
    message = "Cancelled. Confirm the cancellation and ask whether they'd like to book another time."
    if result.late_cancellation:
        message += (
            f" This was within {settings.late_cancellation_hours} hours of the appointment: politely mention the "
            "late cancellation policy from the knowledge base."
        )
    return {
        "ok": True,
        "appointment": appointment_data,
        "late_cancellation": result.late_cancellation,
        "message": message,
    }


@router.post("/request_callback")
async def request_callback(
    body: RequestCallbackIn, db: DbSession, x_session_token: SessionTokenHeader = None
) -> dict[str, Any]:
    session = await _session(db, x_session_token, body)
    if session is None:
        return SESSION_INVALID
    patient = session.patient if session.verified_at else None
    phone_input = body.phone or (patient.phone if patient else None)
    if not phone_input:
        await db.commit()
        return _error("phone_required", "Ask for the best phone number to call them back on.")
    try:
        phone = patients.normalise_phone(phone_input)
    except patients.PatientInputError as exc:
        await db.commit()
        return _error("invalid_input", f"{exc}. Ask them to repeat the number.")

    name = body.name or (f"{patient.first_name} {patient.last_name}" if patient else None)
    callback = CallbackRequest(
        session_id=session.id,
        patient_id=patient.id if patient else None,
        name=name,
        phone=phone,
        reason=body.reason.strip(),
        preferred_time=body.preferred_time,
    )
    db.add(callback)
    await db.flush()
    await publish(db, session.id, "callback_requested", {"preferred_time": body.preferred_time})
    await db.commit()
    return {
        "ok": True,
        "message": "Callback request logged. Tell the patient a member of the team will call them back, usually "
        "within one working day.",
    }


class StartEmergencyCallIn(ToolRequest):
    reason: str | None = Field(default=None, max_length=300)

    _blank = field_validator("reason", mode="before")(blank_to_none)


EMERGENCY_FALLBACK = (
    "No doctor can be reached through the emergency line right now. Tell the patient clearly: if this is "
    "life-threatening, hang up and call 999 now; for urgent medical help that isn't life-threatening, call NHS 111."
)


@router.post("/start_emergency_call")
async def start_emergency_call(
    body: StartEmergencyCallIn,
    db: DbSession,
    factory: SessionFactory,
    telephony: Phone,
    x_session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    """SOS: call every available doctor and connect them with the patient; the AI leaves the call."""
    session = await _session(db, x_session_token, body)
    if session is None:
        return _error("session_invalid", EMERGENCY_FALLBACK)
    channel = "phone" if session.channel == "phone" else "web"
    try:
        incident = await emergency.start_incident(db, telephony, session=session, channel=channel, reason=body.reason or "")
    except emergency.EmergencyUnavailable as exc:
        await db.commit()
        log.warning("Emergency line unavailable (%s) for session %s", exc.code, session.id)
        return _error(exc.code, EMERGENCY_FALLBACK)
    await db.commit()
    emergency.spawn(
        emergency.launch(factory, telephony, incident.id, patient_call_sid=session.call_sid if channel == "phone" else None)
    )
    # The prompt has Sophie say the 999 line before calling this tool; don't make her repeat it.
    if channel == "phone":
        sentence = "I'm connecting you to our doctors right now. If this is life-threatening, hang up and call 999."
        handover = "The call will then be transferred to the doctors."
    else:
        sentence = "I'm connecting you to our doctors right now. If this is life-threatening, call 999."
        handover = "The patient's screen will then join the call with the doctors."
    message = (
        f"The doctors are being called. If you haven't said it yet, say only: '{sentence}' "
        f"Otherwise say only: 'Please stay on the line.' Then stop talking. {handover}"
    )
    return {"ok": True, "incident_id": str(incident.id), "doctors_called": len(incident.calls), "message": message}


class ConnectToReceptionIn(ToolRequest):
    reason: str | None = Field(default=None, max_length=300)

    _blank = field_validator("reason", mode="before")(blank_to_none)


@router.post("/connect_to_reception")
async def connect_to_reception(
    body: ConnectToReceptionIn,
    db: DbSession,
    factory: SessionFactory,
    telephony: Phone,
    x_session_token: SessionTokenHeader = None,
) -> dict[str, Any]:
    """Hand the conversation to a person at reception (phone transfer, or a browser call on the web)."""
    session = await _session(db, x_session_token, body)
    if session is None:
        return SESSION_INVALID
    reason = await handoff.reception_unavailable_reason(db, telephony)
    if reason == "closed":
        await db.commit()
        return _error("reception_closed", "Reception is closed right now. Offer to arrange a callback with request_callback.")
    if reason:
        await db.commit()
        return _error("reception_unavailable", "Calls can't be put through right now. Offer a callback with request_callback.")
    if session.channel == "phone":
        await db.commit()
        emergency.spawn(handoff.transfer_call_to_reception(factory, telephony, session.id))
        return {
            "ok": True,
            "message": "Say only: 'I'll put you through to our reception team now.' Then stop talking - the call will be transferred.",
        }
    await publish(db, session.id, "reception_requested", {"reason": body.reason or ""})
    await db.commit()
    return {
        "ok": True,
        "message": "Say only: 'I'll connect you to our reception team now.' The patient's screen will start the call.",
    }

