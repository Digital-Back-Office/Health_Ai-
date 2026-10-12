"""Shared FastAPI dependencies: auth checks and access to app-wide services."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings, get_settings
from ..db import get_db, get_sessionmaker
from ..models import VoiceSession
from ..services import sessions as session_service
from ..services.calendar import CalendarGateway
from ..services.elevenlabs import ElevenLabsClient, resolve_agent_id
from ..services.events import SessionEventBus

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_calendar(request: Request) -> CalendarGateway:
    return request.app.state.calendar


def get_event_bus(request: Request) -> SessionEventBus:
    return request.app.state.event_bus


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return get_sessionmaker()


def get_elevenlabs(request: Request) -> ElevenLabsClient:
    settings = get_settings()
    agent_id = resolve_agent_id()
    if settings.elevenlabs_api_key is None or not agent_id:
        raise HTTPException(
            status_code=503,
            detail="The voice assistant isn't configured yet (set ELEVENLABS_API_KEY and run `python -m app.cli setup-agent`).",
        )
    return ElevenLabsClient(
        request.app.state.http,
        api_key=settings.elevenlabs_api_key.get_secret_value(),
        agent_id=agent_id,
        base_url=settings.elevenlabs_api_base,
    )


Calendar = Annotated[CalendarGateway, Depends(get_calendar)]
EventBus = Annotated[SessionEventBus, Depends(get_event_bus)]
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
ElevenLabs = Annotated[ElevenLabsClient, Depends(get_elevenlabs)]


def _constant_time_equals(supplied: str | None, expected: str) -> bool:
    return bool(supplied) and secrets.compare_digest(supplied.encode(), expected.encode())


def require_tool_secret(x_tool_secret: Annotated[str | None, Header()] = None) -> None:
    """Tool webhooks must carry the shared secret configured in ElevenLabs."""
    if not _constant_time_equals(x_tool_secret, get_settings().tool_secret.get_secret_value()):
        raise HTTPException(status_code=401, detail="Invalid tool credentials")


def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
    if not _constant_time_equals(token, get_settings().admin_token.get_secret_value()):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def bearer_token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def browser_session(
    db: DbSession, authorization: Annotated[str | None, Header()] = None
) -> VoiceSession:
    """The patient's browser authenticates with the session token it was issued."""
    session = await session_service.resolve_session(db, bearer_token(authorization))
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return session


BrowserSession = Annotated[VoiceSession, Depends(browser_session)]
