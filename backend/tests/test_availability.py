from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import timeutil
from app.models import Appointment, Clinician, Patient
from app.services.availability import find_slots, get_service, is_slot_available
from app.services.calendar import DisabledCalendar

from .conftest import FakeCalendar

PATEL = 1  # seeded clinician ids
WHITFIELD = 2
MONDAY = date(2026, 11, 2)  # GMT, so local time == UTC
SUNDAY_NOON = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


async def _slots(db: AsyncSession, calendar=None, **kwargs):
    service = await get_service(db, kwargs.pop("service_code", "gp_consultation"))
    return await find_slots(db, calendar or DisabledCalendar(), service=service, **kwargs)


def _local_times(slots) -> list[str]:
    return [f"{timeutil.to_clinic(s.starts_at):%H:%M}" for s in slots]


async def test_working_hours_produce_aligned_slots(db: AsyncSession) -> None:
    slots = await _slots(db, date_from=MONDAY, date_to=MONDAY, clinician_id=PATEL, now=SUNDAY_NOON)
    times = _local_times(slots)
    # 20-minute appointments every 15 minutes within 08:30-13:00 and 14:00-18:30
    assert times[0] == "08:30"
    assert "12:30" in times and "12:45" not in times  # 12:45 + 20 min would run past 13:00
    assert "13:00" not in times and "14:00" in times
    assert times[-1] == "18:00"
    assert len(times) == 17 + 17


async def test_minimum_notice_is_respected(db: AsyncSession) -> None:
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    slots = await _slots(db, date_from=MONDAY, date_to=MONDAY, clinician_id=PATEL, now=now)
    assert _local_times(slots)[0] == "11:00"  # two hours' notice


async def test_booked_appointments_block_overlapping_slots(db: AsyncSession) -> None:
    patient = (await db.execute(select(Patient).limit(1))).scalar_one()
    service = await get_service(db, "gp_consultation")
    start = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    db.add(
        Appointment(
            reference="TEST01",
            patient_id=patient.id,
            clinician_id=PATEL,
            service_id=service.id,
            starts_at=start,
            ends_at=start + timedelta(minutes=20),
        )
    )
    await db.commit()
    times = _local_times(await _slots(db, date_from=MONDAY, date_to=MONDAY, clinician_id=PATEL, now=SUNDAY_NOON))
    assert "09:30" in times  # ends 09:50
    assert "09:45" not in times and "10:00" not in times and "10:15" not in times
    assert "10:30" in times


async def test_google_calendar_busy_blocks_are_excluded(db: AsyncSession, fake_calendar: FakeCalendar) -> None:
    clinician = await db.get(Clinician, PATEL)
    clinician.google_calendar_id = "patel@example.test"
    await db.commit()
    fake_calendar.busy["patel@example.test"] = [
        (datetime(2026, 11, 2, 14, 0, tzinfo=UTC), datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    ]
    times = _local_times(
        await _slots(db, fake_calendar, date_from=MONDAY, date_to=MONDAY, clinician_id=PATEL, now=SUNDAY_NOON)
    )
    assert not {"14:00", "14:15", "14:30", "14:45"} & set(times)
    assert "15:00" in times


async def test_part_of_day_filter(db: AsyncSession) -> None:
    slots = await _slots(db, date_from=MONDAY, date_to=MONDAY, clinician_id=PATEL, part_of_day="afternoon", now=SUNDAY_NOON)
    hours = {timeutil.to_clinic(s.starts_at).hour for s in slots}
    assert hours and min(hours) >= 12 and max(hours) < 17


async def test_limit_and_per_day_cap(db: AsyncSession) -> None:
    slots = await _slots(db, date_from=MONDAY, date_to=MONDAY + timedelta(days=6), limit=6, max_per_day=2, now=SUNDAY_NOON)
    assert len(slots) == 6
    per_day: dict[date, int] = {}
    for s in slots:
        day = timeutil.to_clinic(s.starts_at).date()
        per_day[day] = per_day.get(day, 0) + 1
    assert max(per_day.values()) <= 2
    assert slots == sorted(slots, key=lambda s: s.starts_at)


async def test_no_slots_on_sunday_or_beyond_horizon(db: AsyncSession) -> None:
    sunday = date(2026, 11, 8)
    assert await _slots(db, date_from=sunday, date_to=sunday, now=SUNDAY_NOON) == []
    far = MONDAY + timedelta(days=200)
    assert await _slots(db, date_from=far, date_to=far, now=SUNDAY_NOON) == []


async def test_slots_follow_local_time_across_the_clocks_going_back(db: AsyncSession) -> None:
    # Clocks go back on Sunday 25 October 2026.
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    friday_bst = await _slots(db, date_from=date(2026, 10, 23), date_to=date(2026, 10, 23), clinician_id=PATEL, now=now)
    monday_gmt = await _slots(db, date_from=date(2026, 10, 26), date_to=date(2026, 10, 26), clinician_id=PATEL, now=now)
    assert _local_times(friday_bst)[0] == _local_times(monday_gmt)[0] == "08:30"
    assert friday_bst[0].starts_at == datetime(2026, 10, 23, 7, 30, tzinfo=UTC)
    assert monday_gmt[0].starts_at == datetime(2026, 10, 26, 8, 30, tzinfo=UTC)


async def test_is_slot_available_requires_an_exact_slot(db: AsyncSession) -> None:
    service = await get_service(db, "gp_consultation")
    calendar = DisabledCalendar()
    on_grid = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert await is_slot_available(db, calendar, service=service, clinician_id=PATEL, starts_at=on_grid, now=SUNDAY_NOON)
    off_grid = on_grid + timedelta(minutes=7)
    assert not await is_slot_available(db, calendar, service=service, clinician_id=PATEL, starts_at=off_grid, now=SUNDAY_NOON)
    lunchtime = datetime(2026, 11, 2, 13, 15, tzinfo=UTC)
    assert not await is_slot_available(db, calendar, service=service, clinician_id=PATEL, starts_at=lunchtime, now=SUNDAY_NOON)
    # Physiotherapist doesn't offer GP consultations
    assert not await _slots(db, date_from=MONDAY, date_to=MONDAY, clinician_id=3, now=SUNDAY_NOON)
