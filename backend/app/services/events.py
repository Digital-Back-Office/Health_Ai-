"""Live session events for the web UI (slots offered, booking confirmed...).

Tool webhooks run on our backend, but the patient's browser needs to show
what happened (slot cards, confirmation cards). Events are published with
Postgres NOTIFY inside the same transaction as the change they describe, so
they are delivered only if that change commits, and they reach every backend
worker - each worker LISTENs and fans events out to its SSE subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

CHANNEL = "session_events"
# NOTIFY payloads are limited to 8000 bytes; stay well under it.
MAX_PAYLOAD_BYTES = 7000


async def publish(db: AsyncSession, session_id: uuid.UUID, event_type: str, data: dict[str, Any] | None = None) -> None:
    """Queue an event; it is delivered when the surrounding transaction commits."""
    payload = json.dumps({"session_id": str(session_id), "type": event_type, "data": data or {}}, default=str)
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        # Too big for NOTIFY: tell the client to re-fetch the session state instead.
        payload = json.dumps({"session_id": str(session_id), "type": "state_changed", "data": {}})
    await db.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": CHANNEL, "payload": payload})


class SessionEventBus:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="session-event-listener")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=10)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        """Keep one LISTEN connection open, reconnecting with backoff."""
        backoff = 1.0
        while not self._stopping:
            connection: asyncpg.Connection | None = None
            try:
                connection = await asyncpg.connect(self._dsn)
                lost = asyncio.Event()
                connection.add_termination_listener(lambda _conn: lost.set())
                await connection.add_listener(CHANNEL, self._on_notify)
                self._ready.set()
                backoff = 1.0
                log.info("Session event listener connected")
                while not lost.is_set():
                    # Periodic ping detects silently dropped connections.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(lost.wait(), timeout=30)
                    if not lost.is_set():
                        await connection.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - depends on DB outages
                log.warning("Session event listener error: %s (retrying in %.0fs)", exc, backoff)
            finally:
                if connection is not None and not connection.is_closed():
                    with contextlib.suppress(Exception):
                        await connection.close()
            if not self._stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            return
        for queue in list(self._subscribers.get(message.get("session_id", ""), ())):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # drop the oldest event rather than block
            queue.put_nowait({"type": message.get("type"), "data": message.get("data", {})})

    @contextlib.asynccontextmanager
    async def subscribe(self, session_id: uuid.UUID) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        key = str(session_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers[key].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[key].discard(queue)
            if not self._subscribers[key]:
                self._subscribers.pop(key, None)
