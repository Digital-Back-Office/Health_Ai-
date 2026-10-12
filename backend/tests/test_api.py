"""End-to-end tests through HTTP: browser session API + agent tool webhooks + live events."""

import asyncio
import uuid

import httpx
import pytest

from app.config import get_settings

from .conftest import TOOL_HEADERS


async def _start(client: httpx.AsyncClient, mode: str = "voice") -> dict:
    response = await client.post("/api/sessions", json={"mode": mode})
    assert response.status_code == 201, response.text
    return response.json()


def _tool_headers(session: dict) -> dict[str, str]:
    return {**TOOL_HEADERS, "X-Session-Token": session["session_token"]}


async def test_clinic_info(client: httpx.AsyncClient) -> None:
    data = (await client.get("/api/clinic")).json()
    assert data["name"] == get_settings().clinic_name
    assert {s["code"] for s in data["services"]} >= {"gp_consultation", "physio_initial"}
    monday = data["opening_hours"][0]
    assert monday["open"] == "08:00" and monday["close"] == "19:00"
    patel = next(c for c in data["clinicians"] if c["name"] == "Dr Sarah Patel")
    assert {"weekday": 0, "start": "08:30", "end": "13:00"} in patel["working_hours"]
    assert data["opening_hours"][6]["open"] is None  # Sunday closed


async def test_start_session_returns_credentials_and_dynamic_variables(client: httpx.AsyncClient) -> None:
    voice = await _start(client, "voice")
    assert voice["connection_type"] == "webrtc" and voice["conversation_token"].startswith("conv-token-")
    assert voice["dynamic_variables"]["secret__session_token"] == voice["session_token"]
    text = await _start(client, "text")
    assert text["connection_type"] == "websocket" and text["signed_url"].startswith("wss://")


async def test_tools_require_the_shared_secret(client: httpx.AsyncClient) -> None:
    response = await client.post("/tools/verify_patient", json={"last_name": "x", "date_of_birth": "1990-01-01", "phone": "07700900123"})
    assert response.status_code == 401
    response = await client.post("/tools/verify_patient", json={}, headers={"X-Tool-Secret": "wrong"})
    assert response.status_code == 401


async def test_expired_or_unknown_session(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/tools/verify_patient",
        json={"last_name": "Testington", "date_of_birth": "1990-04-12", "phone": "07700 900123"},
        headers={**TOOL_HEADERS, "X-Session-Token": "not-a-real-token"},
    )
    assert response.status_code == 200
    assert response.json()["error"] == "session_invalid"


async def test_invalid_tool_input_is_explained_not_422(client: httpx.AsyncClient) -> None:
    session = await _start(client)
    response = await client.post("/tools/verify_patient", json={"last_name": "Testington"}, headers=_tool_headers(session))
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["error"] == "invalid_input"
    assert "date_of_birth" in body["message"]


async def test_full_booking_journey(client: httpx.AsyncClient) -> None:
    session = await _start(client)
    headers = _tool_headers(session)

    # Booking before identity is confirmed is refused.
    slots = (await client.post("/tools/check_availability", json={"service_code": "gp_consultation"}, headers=headers)).json()
    assert slots["ok"] and slots["slots"], slots
    first = slots["slots"][0]
    refused = (
        await client.post(
            "/tools/book_appointment",
            json={"service_code": "gp_consultation", "clinician_id": first["clinician_id"], "start_time": first["start_time"]},
            headers=headers,
        )
    ).json()
    assert refused["error"] == "identity_not_verified"

    # UK-style date of birth, lower-case surname and an international-format number are accepted.
    verified = (
        await client.post(
            "/tools/verify_patient",
            json={"last_name": "testington", "date_of_birth": "12/04/1990", "phone": "+44 7700 900123"},
            headers=headers,
        )
    ).json()
    assert verified["ok"] and verified["first_name"] == "Alice"

    booked = (
        await client.post(
            "/tools/book_appointment",
            json={"service_code": "gp_consultation", "clinician_id": str(first["clinician_id"]), "start_time": first["start_time"]},
            headers=headers,
        )
    ).json()
    assert booked["ok"], booked
    reference = booked["appointment"]["reference"]
    assert booked["appointment"]["reference_spoken"] == ", ".join(reference)
    assert booked["appointment"]["start_time"] == first["start_time"]

    mine = (await client.post("/tools/get_my_appointments", headers=headers)).json()
    assert [a["reference"] for a in mine["appointments"]] == [reference]

    second = slots["slots"][1]
    moved = (
        await client.post(
            "/tools/reschedule_appointment",
            json={"appointment_reference": reference, "new_start_time": second["start_time"], "clinician_id": second["clinician_id"]},
            headers=headers,
        )
    ).json()
    assert moved["ok"], moved
    assert moved["appointment"]["start_time"] == second["start_time"]

    cancelled = (
        await client.post("/tools/cancel_appointment", json={"appointment_reference": reference, "reason": ""}, headers=headers)
    ).json()
    assert cancelled["ok"] and cancelled["appointment"]["status"] == "cancelled"

    state = (await client.get(f"/api/sessions/{session['session_id']}/state", headers={"Authorization": f"Bearer {session['session_token']}"})).json()
    assert state["verified"] and state["appointments"] == []


async def test_slot_taken_by_another_session_returns_alternatives(client: httpx.AsyncClient) -> None:
    alice, ben = await _start(client), await _start(client)
    await client.post(
        "/tools/verify_patient",
        json={"last_name": "Testington", "date_of_birth": "1990-04-12", "phone": "07700900123"},
        headers=_tool_headers(alice),
    )
    await client.post(
        "/tools/verify_patient",
        json={"last_name": "Sample", "date_of_birth": "1985-11-03", "phone": "07700 900456"},
        headers=_tool_headers(ben),
    )
    slot = (await client.post("/tools/check_availability", json={"service_code": "blood_test"}, headers=_tool_headers(alice))).json()["slots"][0]
    body = {"service_code": "blood_test", "clinician_id": slot["clinician_id"], "start_time": slot["start_time"]}
    first, second = await asyncio.gather(
        client.post("/tools/book_appointment", json=body, headers=_tool_headers(alice)),
        client.post("/tools/book_appointment", json=body, headers=_tool_headers(ben)),
    )
    results = [first.json(), second.json()]
    assert sorted(r["ok"] for r in results) == [False, True]
    loser = next(r for r in results if not r["ok"])
    assert loser["error"] == "slot_unavailable" and loser["alternatives"]


async def test_verification_lockout_via_tools(client: httpx.AsyncClient) -> None:
    session = await _start(client)
    wrong = {"last_name": "Testington", "date_of_birth": "1999-01-01", "phone": "07700900123"}
    codes = [(await client.post("/tools/verify_patient", json=wrong, headers=_tool_headers(session))).json()["error"] for _ in range(3)]
    assert codes == ["no_match", "no_match", "verification_locked"]


async def test_callback_request(client: httpx.AsyncClient) -> None:
    session = await _start(client)
    missing_phone = (await client.post("/tools/request_callback", json={"reason": "Prescription query"}, headers=_tool_headers(session))).json()
    assert missing_phone["error"] == "phone_required"
    ok = (
        await client.post(
            "/tools/request_callback",
            json={"reason": "Prescription query", "phone": "07700 900555", "name": "Pat Caller"},
            headers=_tool_headers(session),
        )
    ).json()
    assert ok["ok"]
    callbacks = (await client.get("/api/admin/callbacks", headers={"Authorization": "Bearer test-admin-token"})).json()["callbacks"]
    assert callbacks[0]["phone"] == "07700900555"


async def test_live_events_are_delivered_on_commit(client: httpx.AsyncClient) -> None:
    from app.main import app

    session = await _start(client)
    session_id = uuid.UUID(session["session_id"])
    async with app.state.event_bus.subscribe(session_id) as queue:
        await client.post(
            "/tools/verify_patient",
            json={"last_name": "Testington", "date_of_birth": "1990-04-12", "phone": "07700900123"},
            headers=_tool_headers(session),
        )
        event = await asyncio.wait_for(queue.get(), timeout=5)
    assert event["type"] == "identity_verified"
    assert event["data"]["first_name"] == "Alice"


async def test_session_capacity_limit(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "max_concurrent_sessions", 2)
    first = await _start(client)
    await _start(client)
    response = await client.post("/api/sessions", json={"mode": "voice"})
    assert response.status_code == 503
    assert "busy" in response.json()["detail"]
    # Ending a conversation frees capacity immediately (sendBeacon-style body token).
    await client.post(f"/api/sessions/{first['session_id']}/end", json={"token": first["session_token"]})
    assert (await client.post("/api/sessions", json={"mode": "voice"})).status_code == 201


async def test_browser_endpoints_check_ownership(client: httpx.AsyncClient) -> None:
    a, b = await _start(client), await _start(client)
    response = await client.post(f"/api/sessions/{b['session_id']}/heartbeat", headers={"Authorization": f"Bearer {a['session_token']}"})
    assert response.status_code == 403
    response = await client.post(f"/api/sessions/{a['session_id']}/heartbeat", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_admin_requires_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/admin/stats")).status_code == 401
    stats = (await client.get("/api/admin/stats", headers={"Authorization": "Bearer test-admin-token"})).json()
    assert "upcoming_appointments" in stats
