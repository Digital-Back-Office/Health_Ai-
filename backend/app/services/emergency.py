"""SOS emergency line.

When a patient presses SOS (web) or tells Sophie it's an emergency (web or phone):

1. An incident is created with a private conference name.
2. Every doctor marked "available for SOS" is phoned at the same time. Each hears
   who needs help and must press 1 to join (so voicemail can never join).
3. The patient is put into the conference: a phone caller's live call is
   redirected out of the AI conversation; a web patient joins from the browser.
   They hear "connecting you to our doctors" and hold music until a doctor joins.
4. Every doctor who presses 1 joins the same call. The AI is not on it.
5. If nobody joins within SOS_MAX_WAIT_SECONDS, the patient is told to call 999.

The system never dials 999 itself; the patient is always told to call 999 if it's
life-threatening.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import timeutil
from ..config import get_settings
from ..models import Clinician, EmergencyCall, EmergencyIncident, EmergencyStatus, VoiceSession
from .events import publish
from .telephony import (
    Telephony,
    TelephonyError,
    from_e164_uk,
    to_e164,
    twiml_doctor_join,
    twiml_doctor_prompt,
    twiml_patient_conference,
    twiml_say_and_hangup,
)

log = logging.getLogger(__name__)

PATIENT_INTRO = (
    "Connecting you to our doctors now. Please stay on the line. "
    "If this is life-threatening, hang up and call 999."
)
UNANSWERED_MESSAGE = (
    "We're sorry, none of our doctors could answer. Please hang up and call 999 now if this is "
    "life-threatening, or call 111 for urgent medical help."
)
# Pause before taking a phone call over, so the agent can finish its sentence.
HANDOVER_DELAY_SECONDS = 2.0
SESSION_EXTENSION = timedelta(hours=1)
FINAL_CALL_STATUSES = {"completed", "no-answer", "busy", "failed", "canceled", "declined", "left"}

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    """Run in the background, keeping a reference so the task isn't garbage-collected."""
    task = asyncio.create_task(coro)
    _tasks.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("Background telephony task failed", exc_info=t.exception())

    task.add_done_callback(_done)


class EmergencyUnavailable(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _url(path: str) -> str:
    return f"{get_settings().public_base_url}{path}"


def urls(incident_id: uuid.UUID, call_id: int | None = None) -> dict[str, str]:
    base = f"/twilio/sos/{incident_id}"
    query = f"?call={call_id}" if call_id is not None else ""
    return {
        "answer": _url(f"{base}/doctor-answer{query}"),
        "accept": _url(f"{base}/doctor-accept{query}"),
        "call_status": _url(f"{base}/call-status{query}"),
        "conference": _url(f"{base}/conference"),
    }


async def available_doctors(db: AsyncSession) -> list[Clinician]:
    rows = await db.execute(
        select(Clinician).where(
            Clinician.is_active.is_(True), Clinician.sos_available.is_(True), Clinician.mobile_phone.is_not(None)
        ).order_by(Clinician.full_name)
    )
    return [c for c in rows.scalars() if (c.mobile_phone or "").strip()]


def patient_summary(incident: EmergencyIncident) -> str:
    """What the doctors hear before pressing 1."""
    if incident.patient is not None:
        p = incident.patient
        who = f"Patient {p.first_name} {p.last_name}, born {p.date_of_birth:%d %B %Y}."
    elif incident.caller_number:
        who = f"A caller on {' '.join(incident.caller_number)}."
    else:
        who = "A patient on the website."
    reason = f" They said: {incident.reason}." if incident.reason else ""
    return f"{who}{reason}"


def incident_payload(incident: EmergencyIncident) -> dict[str, Any]:
    return {
        "incident_id": str(incident.id),
        "status": incident.status,
        "channel": incident.channel,
        "doctors": [
            {"name": call.clinician.full_name if call.clinician else call.to_number, "status": call.status}
            for call in incident.calls
        ],
    }


async def start_incident(
    db: AsyncSession, telephony: Telephony, *, session: VoiceSession | None, channel: str, reason: str = ""
) -> EmergencyIncident:
    """Create the incident and its doctor calls (not dialled yet). Caller commits, then calls launch()."""
    if not telephony.enabled:
        raise EmergencyUnavailable("not_configured", "The emergency line isn't set up.")
    doctors = await available_doctors(db)
    if not doctors:
        raise EmergencyUnavailable("no_doctors_available", "No doctor is marked as available for emergencies right now.")

    patient = session.patient if session is not None and session.verified_at else None
    incident = EmergencyIncident(
        session_id=session.id if session else None,
        patient_id=patient.id if patient else None,
        patient=patient,
        channel=channel,
        reason=(reason or "").strip()[:300],
        caller_number=session.caller_number if session else None,
        status=EmergencyStatus.DIALLING,
        conference_name=f"sos-{uuid.uuid4().hex[:16]}",
        calls=[],
    )
    for doctor in doctors:
        try:
            number = to_e164(doctor.mobile_phone or "")
        except TelephonyError:
            log.warning("Skipping %s: invalid mobile number", doctor.full_name)
            continue
        incident.calls.append(EmergencyCall(clinician_id=doctor.id, clinician=doctor, to_number=number, status="queued"))
    if not incident.calls:
        raise EmergencyUnavailable("no_doctors_available", "No doctor has a valid mobile number for emergencies.")
    db.add(incident)
    await db.flush()
    if session is not None:
        # The browser keeps using the session token for the call itself, which may run long.
        session.expires_at = max(session.expires_at, timeutil.now_utc() + SESSION_EXTENSION)
        await publish(db, session.id, "sos_started", incident_payload(incident))
    return incident


async def _load(db: AsyncSession, incident_id: uuid.UUID) -> EmergencyIncident | None:
    return await db.get(EmergencyIncident, incident_id, populate_existing=True)


async def _notify(db: AsyncSession, incident: EmergencyIncident, event: str = "sos_update") -> None:
    if incident.session_id:
        await publish(db, incident.session_id, event, incident_payload(incident))


async def launch(
    sessionmaker: async_sessionmaker[AsyncSession],
    telephony: Telephony,
    incident_id: uuid.UUID,
    *,
    patient_call_sid: str | None = None,
) -> None:
    """Dial every doctor at once; move a phone patient into the conference; start the 999 timer."""
    settings = get_settings()
    async with sessionmaker() as db:
        incident = await _load(db, incident_id)
        if incident is None or incident.status != EmergencyStatus.DIALLING:
            return  # the patient already left
        summary = patient_summary(incident)

        async def dial(call: EmergencyCall) -> None:
            links = urls(incident.id, call.id)
            try:
                call.call_sid = await telephony.call(
                    call.to_number, url=links["answer"], status_callback=links["call_status"], timeout=settings.sos_ring_seconds
                )
                call.status = "ringing"
            except TelephonyError as exc:
                log.warning("SOS call to %s failed: %s", call.to_number, exc)
                call.status = "failed"

        await asyncio.gather(*(dial(call) for call in incident.calls))
        if all(call.status == "failed" for call in incident.calls):
            incident.status = EmergencyStatus.FAILED
        await _notify(db, incident)
        await db.commit()

        # A short text too, so a doctor who misses the call still has the details.
        for call in incident.calls:
            if call.status != "failed":
                with contextlib.suppress(TelephonyError):
                    await telephony.send_sms(
                        call.to_number,
                        f"{settings.clinic_name} SOS: {summary} We're calling you now - answer and press 1 to join.",
                    )

        if patient_call_sid:
            # Phone patient: give Sophie a moment to finish her sentence, then take the call over.
            await asyncio.sleep(HANDOVER_DELAY_SECONDS)
            twiml = (
                twiml_say_and_hangup(UNANSWERED_MESSAGE)
                if incident.status == EmergencyStatus.FAILED
                else twiml_patient_conference(incident.conference_name, urls(incident.id)["conference"], PATIENT_INTRO)
            )
            try:
                await telephony.update_call(patient_call_sid, twiml)
                incident.patient_call_sid = patient_call_sid
            except TelephonyError as exc:
                # Most likely the caller hung up: don't leave the doctors ringing for nobody.
                log.error("Could not move caller into SOS conference: %s", exc)
                await finish(db, telephony, incident, status=EmergencyStatus.FAILED)
            await db.commit()

    if incident.status == EmergencyStatus.DIALLING:
        spawn(watchdog(sessionmaker, telephony, incident_id))


async def finish(
    db: AsyncSession,
    telephony: Telephony,
    incident: EmergencyIncident,
    *,
    status: EmergencyStatus = EmergencyStatus.ENDED,
    end_conference: bool = True,
) -> None:
    """The patient has gone (hung up, browser call failed): stop ringing the doctors and close the call."""
    if incident.status in (EmergencyStatus.DIALLING, EmergencyStatus.CONNECTED):
        incident.status = status
    incident.ended_at = incident.ended_at or timeutil.now_utc()
    for call in incident.calls:
        if call.status in FINAL_CALL_STATUSES:
            continue
        if call.call_sid:
            with contextlib.suppress(TelephonyError):
                await telephony.hang_up(call.call_sid)
        call.status = "left" if call.status == "joined" else "canceled"
    if end_conference:
        with contextlib.suppress(TelephonyError):
            await telephony.end_conference(incident.conference_name)
    await _notify(db, incident, "sos_ended")


async def watchdog(sessionmaker: async_sessionmaker[AsyncSession], telephony: Telephony, incident_id: uuid.UUID) -> None:
    """If no doctor has joined in time, stop ringing and tell the patient to call 999."""
    await asyncio.sleep(get_settings().sos_max_wait_seconds)
    async with sessionmaker() as db:
        incident = await _load(db, incident_id)
        if incident is None or incident.status != EmergencyStatus.DIALLING:
            return
        incident.status = EmergencyStatus.UNANSWERED
        incident.ended_at = timeutil.now_utc()
        for call in incident.calls:
            if call.status not in FINAL_CALL_STATUSES and call.call_sid:
                with contextlib.suppress(TelephonyError):
                    await telephony.hang_up(call.call_sid)
                call.status = "no-answer"
        if incident.patient_call_sid:
            with contextlib.suppress(TelephonyError):
                await telephony.update_call(incident.patient_call_sid, twiml_say_and_hangup(UNANSWERED_MESSAGE))
        with contextlib.suppress(TelephonyError):
            await telephony.end_conference(incident.conference_name)
        await _notify(db, incident, "sos_unanswered")
        await db.commit()


# --- updates from Twilio callbacks ------------------------------------------------------------


def _call(incident: EmergencyIncident, call_id: int | None = None, call_sid: str | None = None) -> EmergencyCall | None:
    for call in incident.calls:
        if (call_id is not None and call.id == call_id) or (call_sid and call.call_sid == call_sid):
            return call
    return None


async def record_call_status(db: AsyncSession, incident_id: uuid.UUID, call_id: int, twilio_status: str) -> None:
    incident = await _load(db, incident_id)
    call = _call(incident, call_id) if incident else None
    if incident is None or call is None:
        return
    if twilio_status in ("ringing", "initiated", "queued"):
        if call.status in ("queued", "ringing"):
            call.status = "ringing"
    elif twilio_status == "in-progress":
        if call.status in ("queued", "ringing"):
            call.status = "answered"
    elif twilio_status == "completed":
        call.status = {"joined": "left", "answered": "declined"}.get(call.status, "completed")
    else:  # busy, no-answer, failed, canceled
        if call.status not in ("joined", "left"):
            call.status = twilio_status
    await _notify(db, incident)
    await db.commit()


async def doctor_answer_twiml(db: AsyncSession, incident_id: uuid.UUID, call_id: int) -> str:
    incident = await _load(db, incident_id)
    if incident is None or incident.status not in (EmergencyStatus.DIALLING, EmergencyStatus.CONNECTED):
        return twiml_say_and_hangup("This emergency call has already ended. Thank you.")
    return twiml_doctor_prompt(urls(incident.id, call_id)["accept"], patient_summary(incident))


async def doctor_accept_twiml(db: AsyncSession, incident_id: uuid.UUID, call_id: int, digits: str | None) -> str:
    incident = await _load(db, incident_id)
    call = _call(incident, call_id) if incident else None
    if incident is None or call is None:
        return twiml_say_and_hangup("This emergency call could not be found.")
    if digits != "1":
        call.status = "declined"
        await _notify(db, incident)
        await db.commit()
        return twiml_say_and_hangup("Okay. Goodbye.")
    if incident.status not in (EmergencyStatus.DIALLING, EmergencyStatus.CONNECTED):
        return twiml_say_and_hangup("Sorry, the patient has already left the call.")
    call.status = "accepted"
    await _notify(db, incident)
    await db.commit()
    return twiml_doctor_join(incident.conference_name, urls(incident.id)["conference"])


async def record_conference_event(
    db: AsyncSession, telephony: Telephony, incident_id: uuid.UUID, event: str, call_sid: str | None
) -> None:
    incident = await _load(db, incident_id)
    if incident is None:
        return
    doctor_call = _call(incident, call_sid=call_sid)
    if event == "participant-join":
        if doctor_call is not None:
            doctor_call.status = "joined"
            if incident.status == EmergencyStatus.DIALLING:
                incident.status = EmergencyStatus.CONNECTED
                incident.connected_at = timeutil.now_utc()
            await _notify(db, incident, "sos_connected")
        elif call_sid and not incident.patient_call_sid:
            incident.patient_call_sid = call_sid
    elif event == "participant-leave" and doctor_call is not None:
        doctor_call.status = "left"
        await _notify(db, incident)
    elif event == "participant-leave" and call_sid and call_sid == incident.patient_call_sid:
        await finish(db, telephony, incident)  # the patient hung up, perhaps before any doctor answered
    elif event == "conference-end":
        await finish(db, telephony, incident, end_conference=False)
    await db.commit()


async def end_for_patient(db: AsyncSession, telephony: Telephony, incident: EmergencyIncident) -> None:
    """The web patient hung up or their browser couldn't connect."""
    if incident.status in (EmergencyStatus.DIALLING, EmergencyStatus.CONNECTED):
        await finish(db, telephony, incident)
    await db.commit()


async def session_for_call(db: AsyncSession, call_sid: str) -> VoiceSession | None:
    return (await db.execute(select(VoiceSession).where(VoiceSession.call_sid == call_sid))).scalar_one_or_none()


def caller_display(number: str | None) -> str | None:
    return from_e164_uk(number) if number else None
