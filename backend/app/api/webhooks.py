"""ElevenLabs post-call webhook: stores the call outcome, summary and analysis."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import ConversationRecord, VoiceSession
from ..services import sessions as session_service
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
