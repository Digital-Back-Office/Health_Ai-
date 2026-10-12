"""Booking, rescheduling and cancelling appointments.

These functions only flush; the caller commits (after publishing UI events in
the same transaction) and then mirrors the change to Google Calendar via
sync_appointment_calendar(), which runs after the response is sent.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import timeutil
from ..config import get_settings
from ..models import (
    Appointment,
    AppointmentEvent,
    AppointmentStatus,
    CalendarSyncStatus,
    Clinician,
    Patient,
    Service,
    VoiceSession,
)
from .availability import Slot, find_slots, get_service, is_slot_available
from .calendar import CalendarError, CalendarEvent, CalendarGateway, event_id_for

log = logging.getLogger(__name__)

# No 0/O or 1/I: references are read aloud.
REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERENCE_LENGTH = 6


@dataclass
class BookingError(Exception):
    code: str
    message: str
    alternatives: list[Slot] = field(default_factory=list)
    appointments: list[Appointment] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def new_reference() -> str:
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


def normalise_reference(value: str) -> str:
    """'7k-3p 9q' -> '7K3P9Q' (spaces/dashes/commas from speech are dropped)."""
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _is_overlap_violation(exc: IntegrityError) -> bool:
    return "no_overlapping_appointments" in str(exc.orig)


def _is_reference_collision(exc: IntegrityError) -> bool:
    return "appointments_reference_key" in str(exc.orig)


def require_verified_patient(session: VoiceSession) -> Patient:
    if session.patient is None or session.verified_at is None:
        raise BookingError(
            "identity_not_verified",
            "The patient's identity hasn't been confirmed in this conversation. "
            "Use verify_patient (returning patient) or register_patient (new patient) first.",
        )
    return session.patient


async def _service_and_clinician(db: AsyncSession, service_code: str, clinician_id: int) -> tuple[Service, Clinician]:
    service = await get_service(db, service_code)
    if service is None:
        raise BookingError("unknown_service", f"'{service_code}' is not a bookable service at this clinic.")
    clinician = await db.get(Clinician, clinician_id)
    if clinician is None or not clinician.is_active:
        raise BookingError("unknown_clinician", "That clinician isn't available for bookings.")
    if service.id not in {s.id for s in clinician.services}:
        raise BookingError(
            "clinician_does_not_offer_service",
            f"{clinician.full_name} doesn't offer {service.name}. Check availability without a clinician to see who does.",
        )
    return service, clinician


async def suggest_alternatives(
    db: AsyncSession, calendar: CalendarGateway, service: Service, near: datetime, clinician_id: int | None
) -> list[Slot]:
    day = timeutil.to_clinic(near).date()
    for preferred in (clinician_id, None):
        slots = await find_slots(
            db,
            calendar,
            service=service,
            date_from=day,
            date_to=day + timedelta(days=7),
            clinician_id=preferred,
            limit=3,
            max_per_day=2,
        )
        if slots or preferred is None:
            return slots
    return []


async def upcoming_appointments(db: AsyncSession, patient_id: uuid.UUID, now: datetime | None = None) -> list[Appointment]:
    now = now or timeutil.now_utc()
    stmt = (
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status == AppointmentStatus.BOOKED,
            Appointment.starts_at > now,
        )
        .order_by(Appointment.starts_at)
    )
    return list((await db.execute(stmt)).scalars().all())


def _patient_conflict(existing: list[Appointment], start: datetime, end: datetime, ignore: uuid.UUID | None = None) -> Appointment | None:
    for appt in existing:
        if appt.id != ignore and appt.starts_at < end and start < appt.ends_at:
            return appt
    return None


def _initial_sync_status(calendar: CalendarGateway, clinician: Clinician) -> str:
    return CalendarSyncStatus.PENDING if calendar.enabled and clinician.google_calendar_id else CalendarSyncStatus.NOT_REQUIRED


async def book_appointment(
    db: AsyncSession,
    calendar: CalendarGateway,
    *,
    session: VoiceSession,
    service_code: str,
    clinician_id: int,
    starts_at: datetime,
    now: datetime | None = None,
) -> Appointment:
    settings = get_settings()
    patient = require_verified_patient(session)
    service, clinician = await _service_and_clinician(db, service_code, clinician_id)
    starts_at = starts_at.astimezone(UTC)
    ends_at = starts_at + timedelta(minutes=service.duration_minutes)

    existing = await upcoming_appointments(db, patient.id, now)
    if len(existing) >= settings.max_upcoming_appointments_per_patient:
        raise BookingError(
            "too_many_appointments",
            "The patient already has the maximum number of upcoming appointments. Offer to reschedule or cancel one, or a callback.",
            appointments=existing,
        )
    clash = _patient_conflict(existing, starts_at, ends_at)
    if clash is not None:
        raise BookingError(
            "patient_already_booked_then",
            "The patient already has an appointment that overlaps this time.",
            appointments=[clash],
        )

    if not await is_slot_available(
        db, calendar, service=service, clinician_id=clinician.id, starts_at=starts_at, now=now
    ):
        raise BookingError(
            "slot_unavailable",
            "That time is no longer available. Apologise and offer the alternatives instead.",
            alternatives=await suggest_alternatives(db, calendar, service, starts_at, clinician.id),
        )

    clinician_id_value = clinician.id
    for _attempt in range(3):
        appointment = Appointment(
            reference=new_reference(),
            patient=patient,
            clinician=clinician,
            service=service,
            starts_at=starts_at,
            ends_at=ends_at,
            status=AppointmentStatus.BOOKED,
            session_id=session.id,
            calendar_sync_status=_initial_sync_status(calendar, clinician),
        )
        try:
            async with db.begin_nested():
                db.add(appointment)
                await db.flush()
                db.add(
                    AppointmentEvent(
                        appointment_id=appointment.id,
                        action="booked",
                        session_id=session.id,
                        details={"starts_at": starts_at.isoformat(), "clinician_id": clinician.id, "service": service.code},
                    )
                )
            return appointment
        except IntegrityError as exc:
            if _is_overlap_violation(exc):
                # Another session booked the same slot a moment ago.
                raise BookingError(
                    "slot_unavailable",
                    "That time was just taken by someone else. Apologise and offer the alternatives instead.",
                    alternatives=await suggest_alternatives(db, calendar, service, starts_at, clinician_id_value),
                ) from exc
            if not _is_reference_collision(exc):
                raise
    raise RuntimeError("Could not allocate a unique booking reference")


async def find_patient_appointment(
    db: AsyncSession, patient: Patient, reference: str | None, now: datetime | None = None
) -> Appointment:
    now = now or timeutil.now_utc()
    upcoming = await upcoming_appointments(db, patient.id, now)
    if reference:
        ref = normalise_reference(reference)
        for appt in upcoming:
            if appt.reference == ref:
                return appt
        raise BookingError(
            "appointment_not_found",
            f"No upcoming appointment with reference {ref} was found for this patient. "
            "Use get_my_appointments to list their appointments.",
            appointments=upcoming,
        )
    if len(upcoming) == 1:
        return upcoming[0]
    if not upcoming:
        raise BookingError("no_upcoming_appointments", "This patient has no upcoming appointments.")
    raise BookingError(
        "reference_required",
        "The patient has several upcoming appointments. Ask which one they mean.",
        appointments=upcoming,
    )


@dataclass
class RescheduleResult:
    appointment: Appointment
    previous_starts_at: datetime
    previous_clinician: str


async def reschedule_appointment(
    db: AsyncSession,
    calendar: CalendarGateway,
    *,
    session: VoiceSession,
    reference: str | None,
    new_starts_at: datetime,
    clinician_id: int | None = None,
    now: datetime | None = None,
) -> RescheduleResult:
    patient = require_verified_patient(session)
    appointment = await find_patient_appointment(db, patient, reference, now)
    service = appointment.service
    clinician = appointment.clinician
    if clinician_id is not None and clinician_id != clinician.id:
        service, clinician = await _service_and_clinician(db, service.code, clinician_id)

    new_starts_at = new_starts_at.astimezone(UTC)
    new_ends_at = new_starts_at + timedelta(minutes=service.duration_minutes)
    if new_starts_at == appointment.starts_at and clinician.id == appointment.clinician_id:
        raise BookingError("same_time", "That's the time the appointment is already booked for.")

    others = await upcoming_appointments(db, patient.id, now)
    if _patient_conflict(others, new_starts_at, new_ends_at, ignore=appointment.id) is not None:
        raise BookingError("patient_already_booked_then", "The patient already has another appointment at that time.")

    if not await is_slot_available(
        db,
        calendar,
        service=service,
        clinician_id=clinician.id,
        starts_at=new_starts_at,
        exclude_appointment_id=appointment.id,
        now=now,
    ):
        raise BookingError(
            "slot_unavailable",
            "That time isn't available. Offer the alternatives instead.",
            alternatives=await suggest_alternatives(db, calendar, service, new_starts_at, clinician.id),
        )

    previous_start, previous_clinician = appointment.starts_at, appointment.clinician.full_name
    target_clinician_id = clinician.id  # plain value: ORM state may be expired if the savepoint rolls back
    try:
        async with db.begin_nested():
            appointment.starts_at = new_starts_at
            appointment.ends_at = new_ends_at
            appointment.clinician = clinician
            appointment.calendar_sync_status = (
                CalendarSyncStatus.PENDING
                if calendar.enabled and (clinician.google_calendar_id or appointment.synced_calendar_id)
                else CalendarSyncStatus.NOT_REQUIRED
            )
            db.add(
                AppointmentEvent(
                    appointment_id=appointment.id,
                    action="rescheduled",
                    session_id=session.id,
                    details={
                        "from": previous_start.isoformat(),
                        "to": new_starts_at.isoformat(),
                        "clinician_id": clinician.id,
                    },
                )
            )
            await db.flush()
    except IntegrityError as exc:
        if _is_overlap_violation(exc):
            raise BookingError(
                "slot_unavailable",
                "That time was just taken by someone else. Offer the alternatives instead.",
                alternatives=await suggest_alternatives(db, calendar, service, new_starts_at, target_clinician_id),
            ) from exc
        raise
    return RescheduleResult(appointment, previous_start, previous_clinician)


@dataclass
class CancelResult:
    appointment: Appointment
    late_cancellation: bool


async def cancel_appointment(
    db: AsyncSession,
    calendar: CalendarGateway,
    *,
    session: VoiceSession,
    reference: str | None,
    reason: str | None = None,
    now: datetime | None = None,
) -> CancelResult:
    settings = get_settings()
    now = now or timeutil.now_utc()
    patient = require_verified_patient(session)
    appointment = await find_patient_appointment(db, patient, reference, now)
    late = appointment.starts_at - now < timedelta(hours=settings.late_cancellation_hours)
    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = now
    appointment.cancellation_reason = (reason or "").strip()[:500] or None
    appointment.calendar_sync_status = (
        CalendarSyncStatus.PENDING if calendar.enabled and appointment.synced_calendar_id else CalendarSyncStatus.NOT_REQUIRED
    )
    db.add(
        AppointmentEvent(
            appointment_id=appointment.id,
            action="cancelled",
            session_id=session.id,
            details={"late": late, "reason": appointment.cancellation_reason},
        )
    )
    await db.flush()
    return CancelResult(appointment, late)


# --- Google Calendar mirroring -------------------------------------------------


def _calendar_event(appointment: Appointment) -> CalendarEvent:
    patient = appointment.patient
    initials_name = f"{patient.first_name[:1]}. {patient.last_name}"
    return CalendarEvent(
        summary=f"{appointment.service.name} - {initials_name} ({appointment.reference})",
        description=(
            f"Booked by the AI receptionist.\n"
            f"Reference: {appointment.reference}\n"
            f"Patient: {initials_name}\n"
            f"Duration: {appointment.service.duration_minutes} minutes\n\n"
            "Manage this booking in the clinic system - changes made here are not synced back."
        ),
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        private_properties={"appointmentId": str(appointment.id), "reference": appointment.reference},
    )


async def sync_appointment_calendar(
    sessionmaker: async_sessionmaker[AsyncSession], calendar: CalendarGateway, appointment_id: uuid.UUID
) -> str:
    """Make Google Calendar reflect the appointment's current state.

    Serialised per appointment with an advisory lock, so a slow sync can never
    overwrite a newer reschedule. Returns the resulting sync status.
    """
    async with sessionmaker() as db, db.begin():
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"calendar:{appointment_id}"}
        )
        appointment = await db.get(Appointment, appointment_id, populate_existing=True)
        if appointment is None:
            return "missing"
        if not calendar.enabled:
            if appointment.calendar_sync_status in (CalendarSyncStatus.PENDING, CalendarSyncStatus.FAILED):
                appointment.calendar_sync_status = CalendarSyncStatus.NOT_REQUIRED
            return appointment.calendar_sync_status

        target = (
            appointment.clinician.google_calendar_id if appointment.status == AppointmentStatus.BOOKED else None
        )
        current = appointment.synced_calendar_id
        event_id = event_id_for(appointment.id)
        try:
            if current and current != target:
                await calendar.delete_event(current, event_id)
                appointment.synced_calendar_id = None
            if target:
                await calendar.upsert_event(target, event_id, _calendar_event(appointment))
                appointment.synced_calendar_id = target
            appointment.calendar_sync_status = (
                CalendarSyncStatus.SYNCED if (target or current) else CalendarSyncStatus.NOT_REQUIRED
            )
            appointment.calendar_sync_error = None
        except CalendarError as exc:
            log.warning("Calendar sync failed for appointment %s: %s", appointment.reference, exc)
            appointment.calendar_sync_status = CalendarSyncStatus.FAILED
            appointment.calendar_sync_error = str(exc)[:500]
        return appointment.calendar_sync_status


async def resync_pending_calendar_events(
    sessionmaker: async_sessionmaker[AsyncSession], calendar: CalendarGateway, *, limit: int = 50
) -> int:
    """Retry calendar mirroring for appointments that are pending or failed."""
    if not calendar.enabled:
        return 0
    cutoff = timeutil.now_utc() - timedelta(seconds=20)
    async with sessionmaker() as db:
        ids = (
            await db.execute(
                select(Appointment.id)
                .where(
                    Appointment.calendar_sync_status.in_([CalendarSyncStatus.PENDING, CalendarSyncStatus.FAILED]),
                    Appointment.updated_at < cutoff,
                )
                .order_by(Appointment.updated_at)
                .limit(limit)
            )
        ).scalars().all()
    for appointment_id in ids:
        await sync_appointment_calendar(sessionmaker, calendar, appointment_id)
    return len(ids)
