"""SOS emergency line: tools, browser SOS button, Twilio callbacks and the 999 fallback."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import EmergencyIncident

from .conftest import TOOL_HEADERS, TWILIO_HEADERS, FakeTelephony

ADMIN = {"Authorization": "Bearer test-admin-token"}
ALICE = {"last_name": "Testington", "date_of_birth": "1990-04-12", "phone": "07700 900123"}


async def _make_doctors_available(client: httpx.AsyncClient) -> None:
    clinicians = (await client.get("/api/admin/clinicians", headers=ADMIN)).json()["clinicians"]
    patel = next(c for c in clinicians if c["name"] == "Dr Sarah Patel")
    whitfield = next(c for c in clinicians if c["name"] == "Dr James Whitfield")
    for doctor, mobile in ((patel, "07700 900001"), (whitfield, "+44 7700 900002")):
        response = await client.patch(
            f"/api/admin/clinicians/{doctor['id']}", json={"mobile_phone": mobile, "sos_available": True}, headers=ADMIN
        )
        assert response.status_code == 200, response.text


async def _web_session(client: httpx.AsyncClient, verify: bool = True) -> dict:
    session = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    if verify:
        headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
        assert (await client.post("/tools/verify_patient", json=ALICE, headers=headers)).json()["ok"]
    return session


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def _incident(incident_id: str) -> EmergencyIncident:
    async with get_sessionmaker()() as db:
        incident = await db.get(EmergencyIncident, uuid.UUID(incident_id))
        assert incident is not None
        return incident


async def test_admin_cannot_mark_doctor_available_without_a_mobile(client: httpx.AsyncClient) -> None:
    clinicians = (await client.get("/api/admin/clinicians", headers=ADMIN)).json()["clinicians"]
    response = await client.patch(f"/api/admin/clinicians/{clinicians[0]['id']}", json={"sos_available": True}, headers=ADMIN)
    assert response.status_code == 422
    bad = await client.patch(f"/api/admin/clinicians/{clinicians[0]['id']}", json={"mobile_phone": "12"}, headers=ADMIN)
    assert bad.status_code == 422


async def test_web_sos_via_agent_calls_every_available_doctor(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    await _make_doctors_available(client)
    session = await _web_session(client)
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}

    result = (await client.post("/tools/start_emergency_call", json={"reason": "chest pain"}, headers=headers)).json()
    assert result["ok"] and result["doctors_called"] == 2
    await _wait_for(lambda: len(phone.calls) == 2)
    assert {c["to"] for c in phone.calls} == {"+447700900001", "+447700900002"}
    assert all(c["timeout"] == get_settings().sos_ring_seconds for c in phone.calls)
    await _wait_for(lambda: len(phone.sms) == 2)  # doctors also get a text with the details
    assert "Alice Testington" in phone.sms[0][1]
    assert phone.updates == []  # web patient joins from the browser, not by redirect

    incident = await _incident(result["incident_id"])
    assert incident.channel == "web" and incident.status == "dialling" and incident.reason == "chest pain"
    assert {c.status for c in incident.calls} == {"ringing"}


async def test_doctor_presses_1_and_joins_the_patient(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    await _make_doctors_available(client)
    session = await _web_session(client)
    started = (
        await client.post(
            f"/api/sessions/{session['session_id']}/sos",
            json={},
            headers={"Authorization": f"Bearer {session['session_token']}"},
        )
    ).json()
    incident_id = started["incident_id"]
    await _wait_for(lambda: len(phone.calls) == 2)
    incident = await _incident(incident_id)
    doctor_call = incident.calls[0]

    # The doctor answers: they hear who needs help and must press 1.
    answer = await client.post(
        f"/twilio/sos/{incident_id}/doctor-answer?call={doctor_call.id}", data={"CallSid": doctor_call.call_sid}, headers=TWILIO_HEADERS
    )
    assert answer.status_code == 200 and "<Gather" in answer.text and "Alice Testington" in answer.text

    # Pressing anything else declines.
    other = incident.calls[1]
    declined = await client.post(
        f"/twilio/sos/{incident_id}/doctor-accept?call={other.id}", data={"Digits": "9"}, headers=TWILIO_HEADERS
    )
    assert "<Hangup/>" in declined.text

    accepted = await client.post(
        f"/twilio/sos/{incident_id}/doctor-accept?call={doctor_call.id}", data={"Digits": "1"}, headers=TWILIO_HEADERS
    )
    assert "<Conference" in accepted.text and incident.conference_name in accepted.text

    joined = await client.post(
        f"/twilio/sos/{incident_id}/conference",
        data={"StatusCallbackEvent": "participant-join", "CallSid": doctor_call.call_sid},
        headers=TWILIO_HEADERS,
    )
    assert joined.status_code == 204
    incident = await _incident(incident_id)
    assert incident.status == "connected" and incident.connected_at is not None
    statuses = {c.id: c.status for c in incident.calls}
    assert statuses[doctor_call.id] == "joined" and statuses[other.id] == "declined"

    ended = await client.post(
        f"/twilio/sos/{incident_id}/conference", data={"StatusCallbackEvent": "conference-end"}, headers=TWILIO_HEADERS
    )
    assert ended.status_code == 204
    assert (await _incident(incident_id)).status == "ended"

    listing = (await client.get("/api/admin/emergencies", headers=ADMIN)).json()["emergencies"]
    assert listing[0]["incident_id"] == incident_id and listing[0]["patient"] == "Alice Testington"


async def test_sos_button_before_any_conversation_and_browser_join(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    await _make_doctors_available(client)
    started = await client.post("/api/sos", json={})
    assert started.status_code == 201, started.text
    body = started.json()
    auth = {"Authorization": f"Bearer {body['session_token']}"}

    token = (await client.post(f"/api/sessions/{body['session_id']}/voice-token", json={"purpose": "sos"}, headers=auth)).json()
    identity = token["identity"]
    assert token["token"] == f"token-for-{identity}"

    # The browser's call reaches the TwiML app webhook and is put into the conference.
    joined = await client.post(
        "/twilio/voice/client",
        data={"From": f"client:{identity}", "purpose": "sos", "incident": body["incident_id"], "CallSid": "CA-browser"},
        headers=TWILIO_HEADERS,
    )
    assert 'startConferenceOnEnter="false"' in joined.text and "999" in joined.text
    assert (await _incident(body["incident_id"])).patient_call_sid == "CA-browser"

    # Someone else's browser can't join this incident.
    other = await client.post(
        "/twilio/voice/client",
        data={"From": f"client:web-{uuid.uuid4().hex}", "purpose": "sos", "incident": body["incident_id"]},
        headers=TWILIO_HEADERS,
    )
    assert "<Conference" not in other.text


async def test_phone_sos_moves_the_call_into_the_conference(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    await _make_doctors_available(client)
    call = await client.post(
        "/webhooks/elevenlabs/call-start",
        json={"caller_id": "+447700900123", "call_sid": "CA-patient", "conversation_id": "conv-1", "agent_id": "a"},
        headers={"X-Webhook-Secret": "test-tool-secret"},
    )
    token = call.json()["dynamic_variables"]["secret__session_token"]
    headers = {**TOOL_HEADERS, "X-Session-Token": token}
    result = (await client.post("/tools/start_emergency_call", json={"reason": "can't breathe"}, headers=headers)).json()
    assert result["ok"] and "999" in result["message"]
    await _wait_for(lambda: len(phone.updates) == 1)
    call_sid, twiml = phone.updates[0]
    assert call_sid == "CA-patient" and "<Conference" in twiml and "999" in twiml
    incident = await _incident(result["incident_id"])
    assert incident.channel == "phone" and incident.caller_number == "07700900123"


async def test_nobody_answers_patient_told_to_call_999(
    client: httpx.AsyncClient, phone: FakeTelephony, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "sos_max_wait_seconds", 0.2)
    await _make_doctors_available(client)
    call = await client.post(
        "/webhooks/elevenlabs/call-start",
        json={"caller_id": "+447700900456", "call_sid": "CA-ben"},
        headers={"X-Webhook-Secret": "test-tool-secret"},
    )
    headers = {**TOOL_HEADERS, "X-Session-Token": call.json()["dynamic_variables"]["secret__session_token"]}
    result = (await client.post("/tools/start_emergency_call", json={}, headers=headers)).json()
    await _wait_for(lambda: len(phone.updates) >= 2, timeout=4)
    assert "999" in phone.updates[-1][1] and "<Hangup/>" in phone.updates[-1][1]
    assert sorted(phone.hangups) == sorted(c["sid"] for c in phone.calls)  # stop ringing the doctors
    assert (await _incident(result["incident_id"])).status == "unanswered"


async def test_no_doctors_available_falls_back_to_999(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    session = await _web_session(client, verify=False)
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
    result = (await client.post("/tools/start_emergency_call", json={"reason": "fainted"}, headers=headers)).json()
    assert result["ok"] is False and result["error"] == "no_doctors_available" and "999" in result["message"]
    button = await client.post("/api/sos", json={})
    assert button.status_code == 503 and "999" in button.json()["detail"]
    async with get_sessionmaker()() as db:
        assert (await db.execute(select(EmergencyIncident))).first() is None


async def test_emergency_line_not_configured(client: httpx.AsyncClient) -> None:
    session = await _web_session(client, verify=False)
    headers = {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}
    result = (await client.post("/tools/start_emergency_call", json={}, headers=headers)).json()
    assert result["error"] == "not_configured" and "999" in result["message"]


async def test_twilio_webhooks_reject_bad_signatures(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    response = await client.post(f"/twilio/sos/{uuid.uuid4()}/doctor-accept?call=1", data={"Digits": "1"})
    assert response.status_code == 403


async def test_patient_hanging_up_stops_ringing_the_doctors(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    await _make_doctors_available(client)
    started = (await client.post("/api/sos", json={"reason": "collapsed"})).json()
    auth = {"Authorization": f"Bearer {started['session_token']}"}
    base = f"/api/sessions/{started['session_id']}/sos/{started['incident_id']}"
    await _wait_for(lambda: len(phone.calls) == 2)

    status = (await client.get(base, headers=auth)).json()
    assert status["status"] == "dialling" and len(status["doctors"]) == 2

    # Another session can't see or end this call.
    other = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    other_auth = {"Authorization": f"Bearer {other['session_token']}"}
    assert (await client.get(f"/api/sessions/{other['session_id']}/sos/{started['incident_id']}", headers=other_auth)).status_code == 404

    assert (await client.post(f"{base}/end", headers=auth)).status_code == 204
    assert sorted(phone.hangups) == sorted(c["sid"] for c in phone.calls)
    incident = await _incident(started["incident_id"])
    assert incident.status == "ended" and {c.status for c in incident.calls} == {"canceled"}
    assert phone.ended_conferences == [incident.conference_name]


async def test_phone_patient_leaving_the_conference_ends_the_sos(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    await _make_doctors_available(client)
    call = await client.post(
        "/webhooks/elevenlabs/call-start",
        json={"caller_id": "+447700900123", "call_sid": "CA-leaver"},
        headers={"X-Webhook-Secret": "test-tool-secret"},
    )
    headers = {**TOOL_HEADERS, "X-Session-Token": call.json()["dynamic_variables"]["secret__session_token"]}
    result = (await client.post("/tools/start_emergency_call", json={}, headers=headers)).json()
    await _wait_for(lambda: len(phone.updates) == 1)
    left = await client.post(
        f"/twilio/sos/{result['incident_id']}/conference",
        data={"StatusCallbackEvent": "participant-leave", "CallSid": "CA-leaver"},
        headers=TWILIO_HEADERS,
    )
    assert left.status_code == 204
    assert (await _incident(result["incident_id"])).status == "ended"
    assert sorted(phone.hangups) == sorted(c["sid"] for c in phone.calls)


async def test_clinic_info_says_whether_the_emergency_line_works(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/clinic")).json()["emergency_line"] is False  # Twilio not configured


async def test_clinic_info_emergency_line_needs_an_available_doctor(client: httpx.AsyncClient, phone: FakeTelephony) -> None:
    assert (await client.get("/api/clinic")).json()["emergency_line"] is False
    await _make_doctors_available(client)
    assert (await client.get("/api/clinic")).json()["emergency_line"] is True
