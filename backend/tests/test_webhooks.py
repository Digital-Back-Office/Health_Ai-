import hashlib
import hmac
import json
import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.webhooks import verify_signature
from app.models import ConversationRecord, VoiceSession

SECRET = "whsec_test"


def _sign(body: bytes, secret: str = SECRET, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def test_signature_verification() -> None:
    body = b'{"type":"post_call_transcription"}'
    assert verify_signature(body, _sign(body), SECRET)
    assert not verify_signature(body, _sign(body, secret="other"), SECRET)
    assert not verify_signature(body + b" ", _sign(body), SECRET)
    assert not verify_signature(body, _sign(body, timestamp=int(time.time()) - 3600), SECRET)
    assert not verify_signature(body, None, SECRET)
    assert not verify_signature(body, "garbage", SECRET)


async def test_post_call_webhook_stores_analysis_and_ends_session(client: httpx.AsyncClient, db: AsyncSession) -> None:
    started = (await client.post("/api/sessions", json={"mode": "voice"})).json()
    await client.post(
        f"/api/sessions/{started['session_id']}/conversation",
        json={"conversation_id": "conv_123"},
        headers={"Authorization": f"Bearer {started['session_token']}"},
    )
    payload = {
        "type": "post_call_transcription",
        "event_timestamp": int(time.time()),
        "data": {
            "agent_id": "agent_test",
            "conversation_id": "conv_123",
            "status": "done",
            "transcript": [{"role": "agent", "message": "Hello"}],
            "metadata": {"call_duration_secs": 95},
            "analysis": {
                "call_successful": "success",
                "transcript_summary": "Patient booked a GP appointment.",
                "data_collection_results": {"intent": {"value": "book"}},
                "evaluation_criteria_results": {"no_medical_advice": {"result": "success"}},
            },
        },
    }
    body = json.dumps(payload).encode()
    bad = await client.post("/webhooks/elevenlabs", content=body, headers={"elevenlabs-signature": _sign(body, secret="nope")})
    assert bad.status_code == 401

    ok = await client.post("/webhooks/elevenlabs", content=body, headers={"elevenlabs-signature": _sign(body)})
    assert ok.status_code == 200
    # Redelivery is idempotent.
    assert (await client.post("/webhooks/elevenlabs", content=body, headers={"elevenlabs-signature": _sign(body)})).status_code == 200

    records = (await db.execute(select(ConversationRecord))).scalars().all()
    assert len(records) == 1
    record = records[0]
    assert record.summary == "Patient booked a GP appointment." and record.duration_seconds == 95
    assert record.transcript is None  # transcripts are not stored unless STORE_TRANSCRIPTS=true
    session = (await db.execute(select(VoiceSession).where(VoiceSession.conversation_id == "conv_123"))).scalar_one()
    assert session.ended_at is not None and record.session_id == session.id
