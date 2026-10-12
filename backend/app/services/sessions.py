"""Conversation sessions.

A session is created by our backend before the browser connects to
ElevenLabs. Its random token travels to the agent as a `secret__` dynamic
variable and comes back on every tool call in a header, so the backend - not
the LLM - decides which patient a tool call acts for.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import timeutil
from ..config import get_settings
from ..models import VoiceSession


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    db: AsyncSession, *, mode: str = "voice", channel: str = "web", client_ip: str | None = None
) -> tuple[VoiceSession, str]:
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    now = timeutil.now_utc()
    session = VoiceSession(
        token_hash=hash_token(token),
        mode=mode,
        channel=channel,
        client_ip=client_ip,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=settings.session_ttl_minutes),
    )
    db.add(session)
    await db.flush()
    return session, token


async def resolve_session(db: AsyncSession, token: str | None, *, for_update: bool = False) -> VoiceSession | None:
    """Look up a live (unexpired) session by its raw token."""
    if not token:
        return None
    stmt = select(VoiceSession).where(
        VoiceSession.token_hash == hash_token(token),
        VoiceSession.expires_at > timeutil.now_utc(),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def touch(session: VoiceSession, now: datetime | None = None) -> None:
    session.last_seen_at = now or timeutil.now_utc()


async def count_active_sessions(db: AsyncSession, now: datetime | None = None) -> int:
    """Sessions that are still in a live conversation (heartbeat seen recently)."""
    settings = get_settings()
    now = now or timeutil.now_utc()
    stmt = select(func.count(VoiceSession.id)).where(
        VoiceSession.ended_at.is_(None),
        VoiceSession.expires_at > now,
        VoiceSession.last_seen_at > now - timedelta(seconds=settings.session_idle_seconds),
    )
    return int((await db.execute(stmt)).scalar_one())


def end_session(session: VoiceSession) -> None:
    if session.ended_at is None:
        session.ended_at = timeutil.now_utc()


class RateLimiter:
    """Small in-process sliding-window limiter (per key, e.g. client IP).

    Good enough for a single-instance prototype; use Redis or the API gateway
    when running several backend replicas.
    """

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        events = self._events[key]
        while events and now - events[0] > self.window:
            events.popleft()
        if len(events) >= self.max_events:
            return False
        events.append(now)
        return True


def session_public_id(session: VoiceSession) -> uuid.UUID:
    return session.id
