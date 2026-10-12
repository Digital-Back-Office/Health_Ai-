"""Staff endpoints (protected by ADMIN_TOKEN): bookings, callbacks, conversations."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from .. import presenters, timeutil
from ..db import get_sessionmaker
from ..models import (
    Appointment,
    AppointmentStatus,
    CalendarSyncStatus,
    CallbackRequest,
    ConversationRecord,
    VoiceSession,
)
from ..services.booking import resync_pending_calendar_events
from ..services.patients import format_postcode
from .deps import DbSession, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/appointments")
async def list_appointments(
    db: DbSession, date_from: date | None = None, date_to: date | None = None, status: str | None = None
) -> dict[str, Any]:
    today = timeutil.clinic_today()
    date_from = date_from or today
    date_to = date_to or date_from + timedelta(days=13)
    start, end = timeutil.local_day_bounds_utc(date_from, date_to)
    stmt = select(Appointment).where(Appointment.starts_at >= start, Appointment.starts_at < end).order_by(Appointment.starts_at)
    if status:
        stmt = stmt.where(Appointment.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    items = []
    for appt in rows:
        item = presenters.appointment_payload(appt, today)
        patient = appt.patient
        item |= {
            "id": str(appt.id),
            "patient": {
                "name": f"{patient.first_name} {patient.last_name}",
                "date_of_birth": patient.date_of_birth.isoformat(),
                "postcode": format_postcode(patient.postcode) if patient.postcode else None,
                "phone": patient.phone,
            },
            "calendar_sync_status": appt.calendar_sync_status,
            "calendar_sync_error": appt.calendar_sync_error,
            "booked_via": appt.booked_via,
            "created_at": appt.created_at.isoformat(),
            "cancellation_reason": appt.cancellation_reason,
        }
        items.append(item)
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "appointments": items}


@router.get("/callbacks")
async def list_callbacks(db: DbSession, status: str = "open") -> dict[str, Any]:
    rows = (
        await db.execute(
            select(CallbackRequest).where(CallbackRequest.status == status).order_by(CallbackRequest.created_at.desc()).limit(200)
        )
    ).scalars().all()
    return {
        "callbacks": [
            {
                "id": str(c.id),
                "name": c.name,
                "phone": c.phone,
                "reason": c.reason,
                "preferred_time": c.preferred_time,
                "status": c.status,
                "created_at": c.created_at.isoformat(),
                "known_patient": c.patient_id is not None,
            }
            for c in rows
        ]
    }


@router.post("/callbacks/{callback_id}/done")
async def complete_callback(callback_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    callback = await db.get(CallbackRequest, callback_id)
    if callback is None:
        raise HTTPException(status_code=404, detail="Callback not found")
    callback.status = "done"
    await db.commit()
    return {"ok": True}


@router.get("/conversations")
async def list_conversations(db: DbSession, limit: int = 50) -> dict[str, Any]:
    rows = (
        await db.execute(select(ConversationRecord).order_by(ConversationRecord.received_at.desc()).limit(min(limit, 200)))
    ).scalars().all()
    return {
        "conversations": [
            {
                "conversation_id": r.conversation_id,
                "status": r.status,
                "call_successful": r.call_successful,
                "summary": r.summary,
                "duration_seconds": r.duration_seconds,
                "data_collection": {
                    key: (value.get("value") if isinstance(value, dict) else value) for key, value in (r.data_collection or {}).items()
                },
                "evaluation": {
                    key: (value.get("result") if isinstance(value, dict) else value) for key, value in (r.evaluation or {}).items()
                },
                "received_at": r.received_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/stats")
async def stats(db: DbSession) -> dict[str, Any]:
    now = timeutil.now_utc()
    today = timeutil.clinic_today()
    day_start, day_end = timeutil.local_day_bounds_utc(today, today)

    async def count(stmt: Any) -> int:
        return int((await db.execute(stmt)).scalar_one())

    booked = Appointment.status == AppointmentStatus.BOOKED
    return {
        "upcoming_appointments": await count(select(func.count(Appointment.id)).where(booked, Appointment.starts_at > now)),
        "appointments_today": await count(
            select(func.count(Appointment.id)).where(booked, Appointment.starts_at >= day_start, Appointment.starts_at < day_end)
        ),
        "booked_by_assistant_today": await count(
            select(func.count(Appointment.id)).where(Appointment.created_at >= day_start, Appointment.created_at < day_end)
        ),
        "open_callbacks": await count(select(func.count(CallbackRequest.id)).where(CallbackRequest.status == "open")),
        "conversations_today": await count(
            select(func.count(VoiceSession.id)).where(VoiceSession.created_at >= day_start, VoiceSession.created_at < day_end)
        ),
        "calendar_sync_failures": await count(
            select(func.count(Appointment.id)).where(Appointment.calendar_sync_status == CalendarSyncStatus.FAILED)
        ),
    }


@router.post("/calendar/resync")
async def resync_calendar(request: Request) -> dict[str, Any]:
    processed = await resync_pending_calendar_events(get_sessionmaker(), request.app.state.calendar, limit=200)
    return {"ok": True, "processed": processed}
