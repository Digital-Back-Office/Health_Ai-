"""Slot availability.

available = clinician working hours (Postgres)
            - booked appointments (Postgres)
            - busy blocks in the clinician's Google Calendar
            - anything inside the minimum-notice window
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import timeutil
from ..config import get_settings
from ..models import Appointment, AppointmentStatus, Clinician, Service
from .calendar import CalendarGateway

PartOfDay = Literal["any", "morning", "afternoon", "evening"]

_PART_OF_DAY_BOUNDS: dict[str, tuple[time, time]] = {
    "morning": (time(0, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(23, 59, 59)),
}


@dataclass(frozen=True)
class Slot:
    clinician_id: int
    clinician_name: str
    starts_at: datetime  # aware, UTC
    ends_at: datetime


async def get_service(db: AsyncSession, code: str) -> Service | None:
    result = await db.execute(select(Service).where(Service.code == code, Service.is_active.is_(True)))
    return result.scalar_one_or_none()


async def eligible_clinicians(db: AsyncSession, service: Service, clinician_id: int | None = None) -> list[Clinician]:
    stmt = (
        select(Clinician)
        .where(Clinician.is_active.is_(True), Clinician.services.any(Service.id == service.id))
        .order_by(Clinician.full_name)
    )
    if clinician_id is not None:
        stmt = stmt.where(Clinician.id == clinician_id)
    return list((await db.execute(stmt)).scalars().all())


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _in_part_of_day(local_start: datetime, part: PartOfDay) -> bool:
    if part == "any":
        return True
    lower, upper = _PART_OF_DAY_BOUNDS[part]
    return lower <= local_start.time() < upper


async def find_slots(
    db: AsyncSession,
    calendar: CalendarGateway,
    *,
    service: Service,
    date_from: date,
    date_to: date,
    clinician_id: int | None = None,
    part_of_day: PartOfDay = "any",
    limit: int | None = None,
    max_per_day: int | None = None,
    now: datetime | None = None,
    exclude_appointment_id: uuid.UUID | None = None,
) -> list[Slot]:
    settings = get_settings()
    now = now or timeutil.now_utc()
    earliest_start = now + timedelta(minutes=settings.min_notice_minutes)
    today = timeutil.clinic_today(now)
    date_from = max(date_from, today)
    date_to = min(date_to, today + timedelta(days=settings.booking_horizon_days))
    if date_from > date_to:
        return []

    clinicians = await eligible_clinicians(db, service, clinician_id)
    if not clinicians:
        return []

    range_start, range_end = timeutil.local_day_bounds_utc(date_from, date_to)
    busy: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)

    booked = select(Appointment.clinician_id, Appointment.starts_at, Appointment.ends_at).where(
        Appointment.clinician_id.in_([c.id for c in clinicians]),
        Appointment.status == AppointmentStatus.BOOKED,
        Appointment.starts_at < range_end,
        Appointment.ends_at > range_start,
    )
    if exclude_appointment_id is not None:
        booked = booked.where(Appointment.id != exclude_appointment_id)
    for row in (await db.execute(booked)).all():
        busy[row.clinician_id].append((row.starts_at, row.ends_at))

    calendar_owner = {c.google_calendar_id: c.id for c in clinicians if c.google_calendar_id}
    if calendar.enabled and calendar_owner:
        external = await calendar.busy_intervals(list(calendar_owner), range_start, range_end)
        for calendar_id, intervals in external.items():
            owner = calendar_owner.get(calendar_id)
            if owner is not None:
                busy[owner].extend(intervals)

    duration = timedelta(minutes=service.duration_minutes)
    step = timedelta(minutes=settings.slot_step_minutes)
    slots: list[Slot] = []
    day = date_from
    while day <= date_to:
        for clinician in clinicians:
            for window in clinician.working_hours:
                if window.weekday != day.weekday():
                    continue
                window_end = timeutil.local_datetime(day, window.end_time)
                local_start = timeutil.local_datetime(day, window.start_time)
                while local_start + duration <= window_end:
                    start = local_start.astimezone(UTC)
                    end = start + duration
                    if (
                        start >= earliest_start
                        and _in_part_of_day(local_start, part_of_day)
                        and not any(_overlaps(start, end, b0, b1) for b0, b1 in busy[clinician.id])
                    ):
                        slots.append(Slot(clinician.id, clinician.full_name, start, end))
                    local_start += step
        day += timedelta(days=1)

    slots.sort(key=lambda s: (s.starts_at, s.clinician_name))
    if max_per_day is not None:
        per_day: dict[date, int] = defaultdict(int)
        capped: list[Slot] = []
        for slot in slots:
            local_day = timeutil.to_clinic(slot.starts_at).date()
            if per_day[local_day] < max_per_day:
                per_day[local_day] += 1
                capped.append(slot)
        slots = capped
    return slots[:limit] if limit is not None else slots


async def is_slot_available(
    db: AsyncSession,
    calendar: CalendarGateway,
    *,
    service: Service,
    clinician_id: int,
    starts_at: datetime,
    exclude_appointment_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> bool:
    """True if starts_at is exactly one of the bookable slots for that clinician."""
    local_day = timeutil.to_clinic(starts_at).date()
    slots = await find_slots(
        db,
        calendar,
        service=service,
        date_from=local_day,
        date_to=local_day,
        clinician_id=clinician_id,
        now=now,
        exclude_appointment_id=exclude_appointment_id,
    )
    return any(slot.starts_at == starts_at for slot in slots)
