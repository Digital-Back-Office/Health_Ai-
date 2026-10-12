"""Browser-facing session endpoints: start a conversation, live events, heartbeat, end."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from .. import presenters, timeutil
from ..config import get_settings
from ..db import get_sessionmaker
from ..llm import vertex_proxy
from ..models import VoiceSession
from ..services import booking
from ..services import sessions as session_service
from ..services.elevenlabs import ElevenLabsError, session_token_var
from .deps import BrowserSession, DbSession, ElevenLabs, EventBus, bearer_token

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_start_limiter: session_service.RateLimiter | None = None
SESSION_CREATE_LOCK = 724_001  # advisory lock id serialising the concurrency check


def _limiter() -> session_service.RateLimiter:
    global _start_limiter
    if _start_limiter is None:
        _start_limiter = session_service.RateLimiter(get_settings().session_starts_per_minute_per_ip, 60)
    return _start_limiter


class StartSessionIn(BaseModel):
    mode: Literal["voice", "text"] = "voice"


class StartSessionOut(BaseModel):
    session_id: uuid.UUID
    session_token: str
    expires_at: datetime
    mode: str
    connection_type: Literal["webrtc", "websocket"]
    conversation_token: str | None = None
    signed_url: str | None = None
    dynamic_variables: dict[str, str]


class ConversationIn(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)


class EndIn(BaseModel):
    # sendBeacon() can't set headers, so the token may also arrive in the body.
    token: str | None = None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_owner(session: VoiceSession, session_id: uuid.UUID) -> None:
    if session.id != session_id:
        raise HTTPException(status_code=403, detail="Session mismatch")


@router.post("", response_model=StartSessionOut, status_code=201)
async def start_session(body: StartSessionIn, request: Request, db: DbSession, elevenlabs: ElevenLabs) -> StartSessionOut:
    settings = get_settings()
    if not _limiter().allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many attempts - please wait a minute and try again.")

    # Serialise the "how many conversations are live?" check across workers so
    # bursts can't overshoot the ElevenLabs plan's concurrency limit.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SESSION_CREATE_LOCK})
    if await session_service.count_active_sessions(db) >= settings.max_concurrent_sessions:
        raise HTTPException(
            status_code=503,
            detail=f"All of our virtual receptionists are busy right now. Please try again in a minute, "
            f"or call us on {settings.clinic_phone}.",
        )

    try:
        if body.mode == "voice":
            conversation_token, signed_url, connection_type = await elevenlabs.conversation_token(), None, "webrtc"
        else:
            conversation_token, signed_url, connection_type = None, await elevenlabs.signed_url(), "websocket"
    except ElevenLabsError as exc:
        if exc.status_code == 429:
            raise HTTPException(status_code=503, detail="The voice service is at capacity. Please try again shortly.") from exc
        raise HTTPException(status_code=502, detail="Couldn't connect to the voice service. Please try again.") from exc

    session, token = await session_service.create_session(db, mode=body.mode, client_ip=_client_ip(request))
    await db.commit()
    vertex_proxy.schedule_warm_up(request.app.state)
    return StartSessionOut(
        session_id=session.id,
        session_token=token,
        expires_at=session.expires_at,
        mode=body.mode,
        connection_type=connection_type,
        conversation_token=conversation_token,
        signed_url=signed_url,
        dynamic_variables={session_token_var(): token, "channel": "web"},
    )


@router.post("/{session_id}/conversation", status_code=204)
async def bind_conversation(session_id: uuid.UUID, body: ConversationIn, session: BrowserSession, db: DbSession) -> Response:
    _check_owner(session, session_id)
    session.conversation_id = body.conversation_id
    session_service.touch(session)
    await db.commit()
    return Response(status_code=204)


@router.post("/{session_id}/heartbeat")
async def heartbeat(session_id: uuid.UUID, session: BrowserSession, db: DbSession) -> dict[str, Any]:
    _check_owner(session, session_id)
    session_service.touch(session)
    await db.commit()
    return {"expires_at": session.expires_at}


@router.post("/{session_id}/end", status_code=204)
async def end_session(
    session_id: uuid.UUID,
    db: DbSession,
    body: EndIn | None = None,
    authorization: str | None = Header(default=None),
) -> Response:
    token = bearer_token(authorization) or (body.token if body else None)
    session = await session_service.resolve_session(db, token)
    if session is not None and session.id == session_id:
        session_service.end_session(session)
        await db.commit()
    return Response(status_code=204)


@router.get("/{session_id}/state")
async def session_state(session_id: uuid.UUID, session: BrowserSession, db: DbSession) -> dict[str, Any]:
    _check_owner(session, session_id)
    today = timeutil.clinic_today()
    appointments: list[dict[str, Any]] = []
    first_name = None
    if session.patient is not None and session.verified_at is not None:
        first_name = session.patient.first_name
        appointments = [
            presenters.appointment_payload(a, today) for a in await booking.upcoming_appointments(db, session.patient.id)
        ]
    return {
        "verified": first_name is not None,
        "first_name": first_name,
        "appointments": appointments,
        "locked": session.failed_verifications >= get_settings().max_verification_attempts,
    }


@router.get("/{session_id}/events")
async def session_events(
    session_id: uuid.UUID, request: Request, bus: EventBus, authorization: str | None = Header(default=None)
) -> StreamingResponse:
    # Resolve the session with a short-lived DB session: an SSE stream can stay
    # open for many minutes and must not pin a pooled connection.
    async with get_sessionmaker()() as db:
        session = await session_service.resolve_session(db, bearer_token(authorization))
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    _check_owner(session, session_id)
    expires_at = session.expires_at

    async def stream() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        async with bus.subscribe(session_id) as queue:
            yield "event: ready\ndata: {}\n\n"
            while timeutil.now_utc() < expires_at:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'], default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
