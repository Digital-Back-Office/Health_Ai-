"""ElevenLabs post-call webhook: stores the call outcome, summary and analysis."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Appointment, ConversationRecord, Patient, VoiceSession
from ..services import sessions as session_service
from ..services.elevenlabs import session_token_var
from ..services.telephony import TelephonyError, from_e164_uk, to_e164
from .deps import DbSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SIGNATURE_TOLERANCE_SECONDS = 30 * 60


def verify_signature(raw_body: bytes, header: str | None, secret: str, *, now: float | None = None) -> bool:
    """Check an `ElevenLabs-Signature: t=<unix>,v0=<hex hmac-sha256("<t>.<body>")>` header."""
    if not header:
        return False
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp, signature = parts.get("t", ""), parts.get("v0", "")
    if not timestamp.isdigit() or not signature:
        return False
    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


async def store_post_call(db: AsyncSession, data: dict[str, Any]) -> ConversationRecord:
    settings = get_settings()
    conversation_id = str(data.get("conversation_id") or "")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id missing")
    analysis = data.get("analysis") or {}
    metadata = data.get("metadata") or {}

    record = (
        await db.execute(select(ConversationRecord).where(ConversationRecord.conversation_id == conversation_id))
    ).scalar_one_or_none()
    if record is None:
        record = ConversationRecord(conversation_id=conversation_id)
        db.add(record)

    session = (
        await db.execute(select(VoiceSession).where(VoiceSession.conversation_id == conversation_id))
    ).scalar_one_or_none()
    if session is not None:
        record.session_id = session.id
        session_service.end_session(session)

    # Reminder calls: record how the call went on the appointment.
    reminded = (
        await db.execute(select(Appointment).where(Appointment.reminder_conversation_id == conversation_id))
    ).scalar_one_or_none()
    if reminded is not None:
        succeeded = data.get("status") == "done" and analysis.get("call_successful") != "failure"
        reminded.reminder_status = "completed" if succeeded else "failed"

    record.agent_id = data.get("agent_id")
    record.status = data.get("status")
    record.call_successful = analysis.get("call_successful")
    record.summary = analysis.get("transcript_summary")
    duration = metadata.get("call_duration_secs")
    record.duration_seconds = int(duration) if isinstance(duration, int | float) else None
    record.data_collection = analysis.get("data_collection_results") or {}
    record.evaluation = analysis.get("evaluation_criteria_results") or {}
    record.transcript = data.get("transcript") if settings.store_transcripts else None
    await db.commit()
    return record


@router.post("/elevenlabs")
async def elevenlabs_webhook(request: Request, db: DbSession) -> dict[str, Any]:
    settings = get_settings()
    if settings.elevenlabs_webhook_secret is None:
        raise HTTPException(status_code=503, detail="ELEVENLABS_WEBHOOK_SECRET is not configured")
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("elevenlabs-signature"), settings.elevenlabs_webhook_secret.get_secret_value()):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    event_type = payload.get("type")
    if event_type == "post_call_transcription":
        record = await store_post_call(db, payload.get("data") or {})
        log.info("Stored post-call analysis for conversation %s", record.conversation_id)
    else:
        log.info("Ignoring ElevenLabs webhook of type %s", event_type)
    return {"ok": True}


# --- inbound phone calls ----------------------------------------------------------------------


class CallStartIn(BaseModel):
    """What ElevenLabs sends when a call to the clinic's number starts."""

    caller_id: str | None = None
    agent_id: str | None = None
    called_number: str | None = None
    call_sid: str | None = None
    conversation_id: str | None = None


def _caller_number(caller_id: str | None) -> str | None:
    """Normalised caller number, or None when withheld / not a real number."""
    if not caller_id:
        return None
    try:
        return from_e164_uk(to_e164(caller_id))
    except TelephonyError:
        return None


@router.post("/elevenlabs/call-start")
async def call_start(
    body: CallStartIn,
    db: DbSession,
    x_webhook_secret: Annotated[str | None, Header()] = None,
    x_tool_secret: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Create a secure session for an inbound call (ElevenLabs' conversation-initiation webhook)."""
    supplied = x_webhook_secret or x_tool_secret or ""
    expected = get_settings().tool_secret.get_secret_value()
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid webhook credentials")

    caller = _caller_number(body.caller_id)
    session, token = await session_service.create_session(db, mode="voice", channel="phone")
    session.call_sid = body.call_sid
    session.caller_number = caller
    session.conversation_id = body.conversation_id
    known = False
    if caller:
        matches = await db.scalar(select(func.count()).select_from(Patient).where(Patient.phone == caller))
        known = matches == 1
    await db.commit()
    log.info("Inbound call %s: session %s (caller known: %s)", body.call_sid, session.id, known)
    return {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": {
            session_token_var(): token,
            "channel": "phone",
            "caller_known": "yes" if known else "no",
            "call_purpose": "general",
            "appointment_summary": "none",
        },
    }

