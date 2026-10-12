"""Reminder calls the evening before an appointment."""

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models import Appointment, Patient, Service, VoiceSession
from app.services import reminders
from app.services.elevenlabs import ElevenLabsClient, save_state

PATEL = 1
# Monday 2 Nov 2026 is GMT, so clinic time == UTC.
MONDAY_5PM = datetime(2026, 11, 2, 17, 30, tzinfo=UTC)
TUESDAY_10AM = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


async def _appointment(db: AsyncSession, starts_at: datetime, created_at: datetime, reminder_status: str | None = None) -> Appointment:
    patient = (await db.execute(select(Patient).where(Patient.last_name == "Testington"))).scalar_one()
    service = (await db.execute(select(Service).where(Service.code == "gp_consultation"))).scalar_one()
    appointment = Appointment(
        reference=f"R{int(starts_at.timestamp()) % 100000:05d}",
        patient_id=patient.id,
        clinician_id=PATEL,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=20),
        created_at=created_at,
        reminder_status=reminder_status,
    )
    db.add(appointment)
    await db.commit()
    return appointment


async def test_due_reminders_selects_tomorrows_appointments_in_the_evening(db: AsyncSession) -> None:
    due = await _appointment(db, TUESDAY_10AM, created_at=MONDAY_5PM - timedelta(days=3))
    await _appointment(db, TUESDAY_10AM + timedelta(hours=2), created_at=MONDAY_5PM - timedelta(minutes=30))  # just booked
    await _appointment(db, TUESDAY_10AM + timedelta(hours=4), created_at=MONDAY_5PM - timedelta(days=3), reminder_status="completed")
    await _appointment(db, TUESDAY_10AM + timedelta(days=1), created_at=MONDAY_5PM - timedelta(days=3))  # not tomorrow

    assert [a.id for a in await reminders.due_reminders(db, MONDAY_5PM)] == [due.id]
    assert await reminders.due_reminders(db, MONDAY_5PM.replace(hour=15)) == []  # before the call window
    assert await reminders.due_reminders(db, MONDAY_5PM.replace(hour=21)) == []  # too late in the evening


@respx.mock
async def test_place_reminder_call(db: AsyncSession, tmp_path, monkeypatch) -> None:
    from app.services import elevenlabs as el

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(el, "STATE_FILE", state_file)
    save_state({"phone_number_id": "phnum_1", "agent_id": "agent_test"}, state_file)

    appointment = await _appointment(db, datetime.now(UTC) + timedelta(days=1), created_at=datetime.now(UTC) - timedelta(days=2))
    route = respx.post("https://api.elevenlabs.io/v1/convai/twilio/outbound-call").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "conversation_id": "conv-r1", "callSid": "CA-r1"})
    )
    async with httpx.AsyncClient() as http:
        client = ElevenLabsClient(http, api_key="k", agent_id="agent_test", base_url="https://api.elevenlabs.io")
        result = await reminders.place_reminder_call(get_sessionmaker(), client, appointment.id)
    assert result == {"ok": True, "conversation_id": "conv-r1"}

    sent = json.loads(route.calls[0].request.content)
    assert sent["to_number"] == "+447700900123" and sent["agent_phone_number_id"] == "phnum_1"
    variables = sent["conversation_initiation_client_data"]["dynamic_variables"]
    assert variables["call_purpose"] == "reminder" and "Private GP consultation" in variables["appointment_summary"]
    assert "Alice" in sent["conversation_initiation_client_data"]["conversation_config_override"]["agent"]["first_message"]

    async with get_sessionmaker()() as fresh:
        stored = await fresh.get(Appointment, appointment.id)
        assert stored.reminder_status == "calling" and stored.reminder_conversation_id == "conv-r1"
        session = (await fresh.execute(select(VoiceSession).where(VoiceSession.call_sid == "CA-r1"))).scalar_one()
        assert session.purpose == "reminder" and session.verified_at is not None and session.patient_id == stored.patient_id


async def test_post_call_webhook_completes_the_reminder(client: httpx.AsyncClient, db: AsyncSession) -> None:
    from .test_webhooks import _sign

    appointment = await _appointment(db, datetime.now(UTC) + timedelta(days=1), created_at=datetime.now(UTC) - timedelta(days=2))
    appointment.reminder_status = "calling"
    appointment.reminder_conversation_id = "conv-done"
    await db.commit()
    payload = {
        "type": "post_call_transcription",
        "event_timestamp": int(time.time()),
        "data": {"conversation_id": "conv-done", "status": "done", "analysis": {"call_successful": "success"}, "metadata": {}},
    }
    body = json.dumps(payload).encode()
    response = await client.post("/webhooks/elevenlabs", content=body, headers={"elevenlabs-signature": _sign(body)})
    assert response.status_code == 200
    async with get_sessionmaker()() as fresh:
        assert (await fresh.get(Appointment, appointment.id)).reminder_status == "completed"


async def test_reminder_needs_the_phone_line(tmp_path, monkeypatch) -> None:
    from app.services import elevenlabs as el

    monkeypatch.setattr(el, "STATE_FILE", tmp_path / "empty.json")
    async with httpx.AsyncClient() as http:
        client = ElevenLabsClient(http, api_key="k", agent_id="agent_test", base_url="https://api.elevenlabs.io")
        result = await reminders.place_reminder_call(get_sessionmaker(), client, __import__("uuid").uuid4())
    assert result["error"] == "phone_not_set_up"
