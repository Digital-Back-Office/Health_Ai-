import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models import Appointment, AppointmentEvent, AppointmentStatus, CalendarSyncStatus, Clinician
from app.services import booking
from app.services import sessions as session_service
from app.services.booking import BookingError
from app.services.calendar import DisabledCalendar, event_id_for

from .conftest import FakeCalendar, make_verified_session

PATEL, WHITFIELD = 1, 2
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # Sunday; GMT so local == UTC
MONDAY_9 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


async def _book(db: AsyncSession, session, *, start=MONDAY_9, clinician=PATEL, service="gp_consultation", calendar=None):
    appt = await booking.book_appointment(
        db,
        calendar or DisabledCalendar(),
        session=session,
        service_code=service,
        clinician_id=clinician,
        starts_at=start,
        now=NOW,
    )
    await db.commit()
    return appt


async def test_book_appointment(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    appt = await _book(db, session)
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{6}", appt.reference)
    assert appt.status == AppointmentStatus.BOOKED
    assert appt.ends_at - appt.starts_at == timedelta(minutes=20)
    assert appt.calendar_sync_status == CalendarSyncStatus.NOT_REQUIRED
    events = (await db.execute(select(AppointmentEvent).where(AppointmentEvent.appointment_id == appt.id))).scalars().all()
    assert [e.action for e in events] == ["booked"]


async def test_booking_requires_verified_identity(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    await db.commit()
    with pytest.raises(BookingError) as exc:
        await _book(db, session)
    assert exc.value.code == "identity_not_verified"


async def test_off_grid_time_is_rejected_with_alternatives(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    with pytest.raises(BookingError) as exc:
        await _book(db, session, start=MONDAY_9 + timedelta(minutes=7))
    assert exc.value.code == "slot_unavailable"
    assert exc.value.alternatives


async def test_concurrent_bookings_for_the_same_slot_never_double_book() -> None:
    """Two conversations race for one slot: exactly one wins, the other gets alternatives."""
    factory = get_sessionmaker()
    async with factory() as db_a, factory() as db_b:
        session_a, _ = await make_verified_session(db_a, "Testington")
        session_b, _ = await make_verified_session(db_b, "Sample")
        results = await asyncio.gather(_book(db_a, session_a), _book(db_b, session_b), return_exceptions=True)

    successes = [r for r in results if isinstance(r, Appointment)]
    failures = [r for r in results if isinstance(r, BookingError)]
    assert len(successes) == 1 and len(failures) == 1, results
    assert failures[0].code == "slot_unavailable"
    assert failures[0].alternatives

    async with factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(Appointment).where(
                Appointment.clinician_id == PATEL, Appointment.starts_at == MONDAY_9, Appointment.status == "booked"
            )
        )
    assert count == 1


async def test_many_concurrent_bookings_one_winner() -> None:
    factory = get_sessionmaker()
    sessions = []
    for _ in range(8):
        async with factory() as db:
            sessions.append((await make_verified_session(db, "Testington"))[0].id)

    async def attempt(session_id):
        async with factory() as db:
            from app.models import VoiceSession

            session = await db.get(VoiceSession, session_id)
            try:
                return await _book(db, session, start=MONDAY_9 + timedelta(hours=1))
            except BookingError as exc:
                return exc

    results = await asyncio.gather(*(attempt(s) for s in sessions))
    assert sum(isinstance(r, Appointment) for r in results) == 1
    # Same patient in every session: losers fail either on the slot or on their own clash.
    assert all(r.code in {"slot_unavailable", "patient_already_booked_then"} for r in results if isinstance(r, BookingError))


async def test_patient_cannot_be_in_two_places_at_once(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    await _book(db, session, clinician=PATEL)
    with pytest.raises(BookingError) as exc:
        await _book(db, session, clinician=WHITFIELD)
    assert exc.value.code == "patient_already_booked_then"


async def test_clinician_must_offer_service(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    with pytest.raises(BookingError) as exc:
        await _book(db, session, clinician=3)  # physiotherapist
    assert exc.value.code == "clinician_does_not_offer_service"


async def test_reschedule_into_overlapping_time_of_same_appointment(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    appt = await _book(db, session)
    result = await booking.reschedule_appointment(
        db, DisabledCalendar(), session=session, reference=appt.reference.lower(), new_starts_at=MONDAY_9 + timedelta(minutes=15), now=NOW
    )
    await db.commit()
    assert result.appointment.starts_at == MONDAY_9 + timedelta(minutes=15)
    assert result.previous_starts_at == MONDAY_9
    assert result.appointment.reference == appt.reference


async def test_reschedule_without_reference_when_only_one_appointment(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    await _book(db, session)
    result = await booking.reschedule_appointment(
        db, DisabledCalendar(), session=session, reference=None, new_starts_at=MONDAY_9 + timedelta(hours=2), clinician_id=WHITFIELD, now=NOW
    )
    await db.commit()
    assert result.appointment.clinician_id == WHITFIELD
    assert result.previous_clinician == "Dr Sarah Patel"


async def test_cancel_flags_late_cancellations(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    appt = await _book(db, session)
    late = await booking.cancel_appointment(
        db, DisabledCalendar(), session=session, reference=appt.reference, reason="Feeling better", now=MONDAY_9 - timedelta(hours=3)
    )
    await db.commit()
    assert late.late_cancellation is True
    assert late.appointment.status == AppointmentStatus.CANCELLED

    appt2 = await _book(db, session, start=MONDAY_9 + timedelta(days=2))
    early = await booking.cancel_appointment(db, DisabledCalendar(), session=session, reference=appt2.reference, now=NOW)
    await db.commit()
    assert early.late_cancellation is False


async def test_cancelled_slot_becomes_bookable_again(db: AsyncSession) -> None:
    session, _ = await make_verified_session(db)
    appt = await _book(db, session)
    await booking.cancel_appointment(db, DisabledCalendar(), session=session, reference=appt.reference, now=NOW)
    await db.commit()
    other, _ = await make_verified_session(db, "Sample")
    rebooked = await _book(db, other)
    assert rebooked.starts_at == MONDAY_9


async def test_cannot_act_on_someone_elses_appointment(db: AsyncSession) -> None:
    alice, _ = await make_verified_session(db, "Testington")
    appt = await _book(db, alice)
    ben, _ = await make_verified_session(db, "Sample")
    with pytest.raises(BookingError) as exc:
        await booking.cancel_appointment(db, DisabledCalendar(), session=ben, reference=appt.reference, now=NOW)
    assert exc.value.code in {"appointment_not_found", "no_upcoming_appointments"}


async def test_calendar_mirroring_book_move_cancel(db: AsyncSession, fake_calendar: FakeCalendar) -> None:
    for clinician_id, calendar_id in ((PATEL, "patel@cal"), (WHITFIELD, "whitfield@cal")):
        (await db.get(Clinician, clinician_id)).google_calendar_id = calendar_id
    await db.commit()
    session, _ = await make_verified_session(db)
    factory = get_sessionmaker()

    appt = await _book(db, session, calendar=fake_calendar)
    assert appt.calendar_sync_status == CalendarSyncStatus.PENDING
    assert await booking.sync_appointment_calendar(factory, fake_calendar, appt.id) == CalendarSyncStatus.SYNCED
    event_id = event_id_for(appt.id)
    assert ("patel@cal", event_id) in fake_calendar.events
    assert "Testington" in fake_calendar.events[("patel@cal", event_id)].summary

    await booking.reschedule_appointment(
        db, fake_calendar, session=session, reference=appt.reference, new_starts_at=MONDAY_9 + timedelta(hours=2), clinician_id=WHITFIELD, now=NOW
    )
    await db.commit()
    await booking.sync_appointment_calendar(factory, fake_calendar, appt.id)
    assert ("patel@cal", event_id) not in fake_calendar.events
    assert ("whitfield@cal", event_id) in fake_calendar.events

    await booking.cancel_appointment(db, fake_calendar, session=session, reference=appt.reference, now=NOW)
    await db.commit()
    await booking.sync_appointment_calendar(factory, fake_calendar, appt.id)
    assert not fake_calendar.events


async def test_calendar_failures_are_retried(db: AsyncSession, fake_calendar: FakeCalendar) -> None:
    (await db.get(Clinician, PATEL)).google_calendar_id = "patel@cal"
    await db.commit()
    session, _ = await make_verified_session(db)
    factory = get_sessionmaker()
    appt = await _book(db, session, calendar=fake_calendar)

    fake_calendar.fail = True
    assert await booking.sync_appointment_calendar(factory, fake_calendar, appt.id) == CalendarSyncStatus.FAILED
    fake_calendar.fail = False
    # Pretend the failure happened a while ago so the resync job picks it up.
    async with factory() as s:
        row = await s.get(Appointment, appt.id)
        row.updated_at = datetime.now(UTC) - timedelta(minutes=5)
        await s.commit()
    assert await booking.resync_pending_calendar_events(factory, fake_calendar) == 1
    async with factory() as s:
        assert (await s.get(Appointment, appt.id)).calendar_sync_status == CalendarSyncStatus.SYNCED
