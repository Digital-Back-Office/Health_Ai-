"""Inbound calls (call-start webhook), caller-ID verification, reception hand-off and SMS confirmations."""

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import AppointmentEvent, CallbackRequest, VoiceSession
from app.services import handoff

from .conftest import TOOL_HEADERS, TWILIO_HEADERS, FakeTelephony

WEBHOOK = {"X-Webhook-Secret": "test-tool-secret"}


async def _inbound_call(client: httpx.AsyncClient, caller: str, call_sid: str = "CA-in") -> dict:
    response = await client.post(
        "/webhooks/elevenlabs/call-start",
        json={"caller_id": caller, "agent_id": "agent_test", "called_number": "+442079460000", "call_sid": call_sid, "conversation_id": f"conv-{call_sid}"},
        headers=WEBHOOK,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_call_start_requires_the_secret(client: httpx.AsyncClient) -> None:
    response = await client.post("/webhooks/elevenlabs/call-start", json={"caller_id": "+447700900123"})
    assert response.status_code == 401


async def test_call_start_creates_a_phone_session(client: httpx.AsyncClient) -> None:
    known = await _inbound_call(client, "+447700900123", "CA-known")
    assert known["type"] == "conversation_initiation_client_data"
    variables = known["dynamic_variables"]
    assert variables["channel"] == "phone" and variables["caller_known"] == "yes"
    assert variables["secret__session_token"] and variables["call_purpose"] == "general"

    unknown = await _inbound_call(client, "+447700900999", "CA-unknown")
    assert unknown["dynamic_variables"]["caller_known"] == "no"
    withheld = await _inbound_call(client, "anonymous", "CA-withheld")
    assert withheld["dynamic_variables"]["caller_known"] == "no"

    async with get_sessionmaker()() as db:
        session = (await db.execute(select(VoiceSession).where(VoiceSession.call_sid == "CA-known"))).scalar_one()
        assert session.channel == "phone" and session.caller_number == "07700900123"
        assert session.conversation_id == "conv-CA-known"


async def test_known_caller_only_needs_surname_and_birthday(client: httpx.AsyncClient) -> None:
    call = await _inbound_call(client, "+447700900123")
    headers = {**TOOL_HEADERS, "X-Session-Token": call["dynamic_variables"]["secret__session_token"]}
    verified = (await client.post("/tools/verify_patient", json={"last_name": "Testington", "date_of_birth": "12/04/1990"}, headers=headers)).json()
    assert verified["ok"] and verified["first_name"] == "Alice"


async def test_web_verification_still_needs_the_phone_number(client: httpx.AsyncClient) -> None:
    session = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
    result = (await client.post("/tools/verify_patient", json={"last_name": "Testington", "date_of_birth": "1990-04-12"}, headers=headers)).json()
    assert result["error"] == "phone_required"


async def test_phone_call_transferred_to_reception(
    client: httpx.AsyncClient, phone: FakeTelephony, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "reception_phone_number", "+442070000001")

    async def open_now(db, now=None):
        return True

    monkeypatch.setattr(handoff, "clinic_open_now", open_now)
    call = await _inbound_call(client, "+447700900123", "CA-reception")
    headers = {**TOOL_HEADERS, "X-Session-Token": call["dynamic_variables"]["secret__session_token"]}
    result = (await client.post("/tools/connect_to_reception", json={}, headers=headers)).json()
    assert result["ok"]
    await _wait_for(lambda: len(phone.updates) == 1)
    sid, twiml = phone.updates[0]
    assert sid == "CA-reception" and "<Number>+442070000001</Number>" in twiml

    # Nobody picks up at reception: the caller is told and a callback request is logged.
    async with get_sessionmaker()() as db:
        session = (await db.execute(select(VoiceSession).where(VoiceSession.call_sid == "CA-reception"))).scalar_one()
    after = await client.post(f"/twilio/reception/{session.id}/after-dial", data={"DialCallStatus": "no-answer"}, headers=TWILIO_HEADERS)
    assert "call you back" in after.text
    async with get_sessionmaker()() as db:
        callback = (await db.execute(select(CallbackRequest))).scalar_one()
        assert callback.phone == "07700900123"


async def test_reception_closed_or_not_configured(client: httpx.AsyncClient, phone: FakeTelephony, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    session = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
    not_set = (await client.post("/tools/connect_to_reception", json={}, headers=headers)).json()
    assert not_set["error"] == "reception_unavailable"

    monkeypatch.setattr(get_settings(), "reception_phone_number", "+442070000001")

    async def closed(db, now=None):
        return False

    monkeypatch.setattr(handoff, "clinic_open_now", closed)
    result = (await client.post("/tools/connect_to_reception", json={}, headers=headers)).json()
    assert result["error"] == "reception_closed" and "request_callback" in result["message"]


async def test_web_reception_request_asks_the_browser_to_call(
    client: httpx.AsyncClient, phone: FakeTelephony, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings
    from app.main import app

    monkeypatch.setattr(get_settings(), "reception_phone_number", "+442070000001")

    async def open_now(db, now=None):
        return True

    monkeypatch.setattr(handoff, "clinic_open_now", open_now)
    session = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
    import uuid

    async with app.state.event_bus.subscribe(uuid.UUID(session["session_id"])) as queue:
        result = (await client.post("/tools/connect_to_reception", json={"reason": "billing"}, headers=headers)).json()
        event = await asyncio.wait_for(queue.get(), timeout=5)
    assert result["ok"] and event["type"] == "reception_requested"
    assert phone.updates == []

    identity = f"web-{uuid.UUID(session['session_id']).hex}"
    dial = await client.post("/twilio/voice/client", data={"From": f"client:{identity}", "purpose": "reception"}, headers=TWILIO_HEADERS)
    assert "<Number>+442070000001</Number>" in dial.text


async def test_booking_sends_an_sms_confirmation(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    session = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
    await client.post("/tools/verify_patient", json={"last_name": "Testington", "date_of_birth": "1990-04-12", "phone": "07700900123"}, headers=headers)
    slot = (await client.post("/tools/check_availability", json={"service_code": "gp_consultation"}, headers=headers)).json()["slots"][0]
    booked = (
        await client.post(
            "/tools/book_appointment",
            json={"service_code": "gp_consultation", "clinician_id": slot["clinician_id"], "start_time": slot["start_time"]},
            headers=headers,
        )
    ).json()
    assert booked["ok"] and "text message" in booked["message"]
    await _wait_for(lambda: len(phone.sms) == 1)
    to, body = phone.sms[0]
    assert to == "+447700900123"
    assert booked["appointment"]["reference"] in body and "booked" in body and "Private GP consultation" in body

    cancelled = (await client.post("/tools/cancel_appointment", json={}, headers=headers)).json()
    assert cancelled["ok"]
    await _wait_for(lambda: len(phone.sms) == 2)
    assert "cancelled" in phone.sms[1][1]
    async with get_sessionmaker()() as db:
        actions = [e.action for e in (await db.execute(select(AppointmentEvent).order_by(AppointmentEvent.id))).scalars()]
    assert actions.count("sms_sent") == 2


async def test_sms_failure_is_recorded_not_raised(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    phone.fail_sms = True
    session = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
    await client.post("/tools/verify_patient", json={"last_name": "Sample", "date_of_birth": "1985-11-03", "phone": "07700900456"}, headers=headers)
    slot = (await client.post("/tools/check_availability", json={"service_code": "blood_test"}, headers=headers)).json()["slots"][0]
    booked = (
        await client.post(
            "/tools/book_appointment",
            json={"service_code": "blood_test", "clinician_id": slot["clinician_id"], "start_time": slot["start_time"]},
            headers=headers,
        )
    ).json()
    assert booked["ok"]

    async def failure_recorded() -> bool:
        async with get_sessionmaker()() as db:
            return (await db.execute(select(AppointmentEvent).where(AppointmentEvent.action == "sms_failed"))).first() is not None

    for _ in range(100):
        if await failure_recorded():
            break
        await asyncio.sleep(0.02)
    assert await failure_recorded()
