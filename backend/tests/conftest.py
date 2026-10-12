"""Test fixtures. Tests run against a real Postgres (docker compose `db`, database `healthcare_test`)
because the double-booking guarantee lives in a Postgres exclusion constraint."""

from __future__ import annotations

import os

os.environ.update(
    {
        "APP_ENV_FILE": "",  # never read the developer's backend/.env during tests
        "DATABASE_URL": os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://healthcare:healthcare@localhost:5433/healthcare_test"
        ),
        "ENVIRONMENT": "test",
        "GOOGLE_CALENDAR_ENABLED": "false",
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "ELEVENLABS_AGENT_ID": "agent_test",
        "ELEVENLABS_WEBHOOK_SECRET": "whsec_test",
        "ELEVENLABS_SESSION_TOKEN_VAR": "secret__session_token",
        "VERTEX_PROJECT_ID": "test-project",
        "VERTEX_LOCATION": "europe-west2",
        "VERTEX_MODEL": "google/gemini-3.5-flash",
        "TOOL_SECRET": "test-tool-secret",
        "LLM_PROXY_SECRET": "test-llm-secret",
        "ADMIN_TOKEN": "test-admin-token",
        "MAX_CONCURRENT_SESSIONS": "50",
        "SESSION_STARTS_PER_MINUTE_PER_IP": "1000",
        "LOG_LEVEL": "WARNING",
    }
)

import uuid  # noqa: E402
from collections.abc import AsyncIterator, Sequence  # noqa: E402
from datetime import datetime  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.models import Base, Patient, VoiceSession  # noqa: E402
from app.seed import seed  # noqa: E402
from app.services import sessions as session_service  # noqa: E402
from app.services.calendar import BusyMap, CalendarError, CalendarEvent  # noqa: E402

TOOL_HEADERS = {"X-Tool-Secret": "test-tool-secret"}


class FakeCalendar:
    """In-memory stand-in for Google Calendar."""

    enabled = True

    def __init__(self) -> None:
        self.busy: dict[str, list[tuple[datetime, datetime]]] = {}
        self.events: dict[tuple[str, str], CalendarEvent] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.fail = False

    async def busy_intervals(self, calendar_ids: Sequence[str], start: datetime, end: datetime) -> BusyMap:
        if self.fail:
            raise CalendarError("calendar down")
        return {cid: [b for b in self.busy.get(cid, []) if b[0] < end and start < b[1]] for cid in calendar_ids}

    async def upsert_event(self, calendar_id: str, event_id: str, event: CalendarEvent) -> None:
        if self.fail:
            raise CalendarError("calendar down")
        self.calls.append(("upsert", calendar_id, event_id))
        self.events[(calendar_id, event_id)] = event

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        if self.fail:
            raise CalendarError("calendar down")
        self.calls.append(("delete", calendar_id, event_id))
        self.events.pop((calendar_id, event_id), None)


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture(autouse=True)
async def _clean_data() -> AsyncIterator[None]:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with get_sessionmaker()() as db:
        await db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await db.commit()
        await seed(db, demo_patients=True)
    yield


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def fake_calendar() -> FakeCalendar:
    return FakeCalendar()


async def make_verified_session(db: AsyncSession, last_name: str = "Testington") -> tuple[VoiceSession, str]:
    """A session already bound to one of the seeded test patients."""
    from sqlalchemy import select

    from app import timeutil

    session, token = await session_service.create_session(db)
    patient = (await db.execute(select(Patient).where(Patient.last_name == last_name))).scalar_one()
    session.patient = patient
    session.patient_id = patient.id
    session.verified_at = timeutil.now_utc()
    await db.commit()
    return session, token


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client against the real app (lifespan included), with ElevenLabs faked."""
    from app.api.deps import get_elevenlabs
    from app.main import app, lifespan

    class FakeElevenLabs:
        agent_id = "agent_test"

        async def conversation_token(self) -> str:
            return "conv-token-" + uuid.uuid4().hex[:6]

        async def signed_url(self) -> str:
            return "wss://example.test/signed"

    app.dependency_overrides[get_elevenlabs] = lambda: FakeElevenLabs()
    async with lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
    app.dependency_overrides.clear()
