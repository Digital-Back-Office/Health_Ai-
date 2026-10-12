"""Webhooks Twilio calls during SOS conferences, reception transfers and browser calls.

Every request is checked against Twilio's X-Twilio-Signature (needs TWILIO_AUTH_TOKEN).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..config import get_settings
from ..models import CallbackRequest, EmergencyIncident, EmergencyStatus, VoiceSession
from ..services import emergency
from ..services.handoff import NO_ANSWER_MESSAGE, reception_twiml, reception_unavailable_reason
from ..services.telephony import twiml_patient_conference, twiml_say_and_hangup
from .deps import DbSession, Phone

router = APIRouter(prefix="/twilio", tags=["twilio"])

BROWSER_IDENTITY_PREFIX = "web-"


def browser_identity(session_id: uuid.UUID) -> str:
    return f"{BROWSER_IDENTITY_PREFIX}{session_id.hex}"


def _xml(twiml: str) -> Response:
    return Response(content=twiml, media_type="application/xml")


async def twilio_params(request: Request, telephony: Phone) -> dict[str, str]:
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    # Twilio signs the public URL it called (the tunnel / proxy), not our local one.
    url = f"{get_settings().public_base_url}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    if not telephony.validate_request(url, params, request.headers.get("X-Twilio-Signature")):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return params


TwilioParams = Annotated[dict[str, str], Depends(twilio_params)]


async def _browser_session(db: DbSession, params: dict[str, str]) -> VoiceSession | None:
    identity = params.get("From", "").removeprefix("client:")
    if not identity.startswith(BROWSER_IDENTITY_PREFIX):
        return None
    try:
        session_id = uuid.UUID(hex=identity.removeprefix(BROWSER_IDENTITY_PREFIX))
    except ValueError:
        return None
    return await db.get(VoiceSession, session_id)


@router.post("/voice/client")
async def browser_call(params: TwilioParams, db: DbSession, telephony: Phone) -> Response:
    """TwiML app voice URL: a web patient's browser joining an SOS call or calling reception."""
    session = await _browser_session(db, params)
    if session is None:
        return _xml(twiml_say_and_hangup("Sorry, this call could not be connected."))

    purpose = params.get("purpose", "")
    if purpose == "sos":
        try:
            incident = await db.get(EmergencyIncident, uuid.UUID(params.get("incident", "")))
        except ValueError:
            incident = None
        if incident is None or incident.session_id != session.id:
            return _xml(twiml_say_and_hangup("Sorry, this emergency call could not be found. If you need urgent help, call 999."))
        if incident.status not in (EmergencyStatus.DIALLING, EmergencyStatus.CONNECTED):
            return _xml(twiml_say_and_hangup(emergency.UNANSWERED_MESSAGE))
        incident.patient_call_sid = params.get("CallSid")
        await db.commit()
        return _xml(
            twiml_patient_conference(incident.conference_name, emergency.urls(incident.id)["conference"], emergency.PATIENT_INTRO)
        )

    if purpose == "reception":
        reason = await reception_unavailable_reason(db, telephony)
        if reason == "closed":
            return _xml(twiml_say_and_hangup("Sorry, our reception is closed right now. Please ask Sophie to arrange a callback."))
        if reason:
            return _xml(twiml_say_and_hangup("Sorry, calls to reception aren't available right now."))
        return _xml(reception_twiml(telephony, session.id))

    return _xml(twiml_say_and_hangup("Sorry, this call could not be connected."))


@router.post("/sos/{incident_id}/doctor-answer")
async def doctor_answer(incident_id: uuid.UUID, call: int, params: TwilioParams, db: DbSession) -> Response:
    return _xml(await emergency.doctor_answer_twiml(db, incident_id, call))


@router.post("/sos/{incident_id}/doctor-accept")
async def doctor_accept(incident_id: uuid.UUID, call: int, params: TwilioParams, db: DbSession) -> Response:
    return _xml(await emergency.doctor_accept_twiml(db, incident_id, call, params.get("Digits")))


@router.post("/sos/{incident_id}/call-status", status_code=204)
async def doctor_call_status(incident_id: uuid.UUID, call: int, params: TwilioParams, db: DbSession) -> Response:
    await emergency.record_call_status(db, incident_id, call, params.get("CallStatus", ""))
    return Response(status_code=204)


@router.post("/sos/{incident_id}/conference", status_code=204)
async def conference_events(incident_id: uuid.UUID, params: TwilioParams, db: DbSession, telephony: Phone) -> Response:
    await emergency.record_conference_event(
        db, telephony, incident_id, params.get("StatusCallbackEvent", ""), params.get("CallSid")
    )
    return Response(status_code=204)


@router.post("/reception/{session_id}/after-dial")
async def reception_after_dial(session_id: uuid.UUID, params: TwilioParams, db: DbSession) -> Response:
    if params.get("DialCallStatus") == "completed":
        return _xml("<Response><Hangup/></Response>")
    # Nobody answered: make sure the team calls the patient back.
    session = await db.get(VoiceSession, session_id)
    if session is not None:
        patient = session.patient if session.verified_at else None
        phone = session.caller_number or (patient.phone if patient else None)
        if phone:
            db.add(
                CallbackRequest(
                    session_id=session.id,
                    patient_id=patient.id if patient else None,
                    name=f"{patient.first_name} {patient.last_name}" if patient else None,
                    phone=phone,
                    reason="Asked to speak to reception, but nobody could answer.",
                )
            )
            await db.commit()
    return _xml(twiml_say_and_hangup(NO_ANSWER_MESSAGE))
