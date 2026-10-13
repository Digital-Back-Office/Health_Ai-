"""FastAPI application: tool webhooks, browser API, LLM proxy, staff API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .api import admin, clinic, sessions, tools, twilio_webhooks, webhooks
from .api.deps import elevenlabs_client
from .config import BACKEND_DIR, Settings, get_settings, refresh_public_base_url
from .db import dispose_engine, get_sessionmaker
from .llm import vertex_proxy
from .services.booking import resync_pending_calendar_events
from .services.calendar import build_calendar_gateway
from .services.elevenlabs import load_state
from .services.events import SessionEventBus
from .services.reminders import run_due_reminders
from .services.telephony import build_telephony

log = logging.getLogger("app")

FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"


async def _calendar_resync_loop(app: FastAPI, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            processed = await resync_pending_calendar_events(get_sessionmaker(), app.state.calendar)
            if processed:
                log.info("Calendar resync processed %d appointment(s)", processed)
        except Exception:
            log.exception("Calendar resync loop failed")


async def _public_url_watch_loop(settings: Settings, env_file: str, interval: float = 2.0) -> None:
    """Development: follow PUBLIC_BASE_URL when dev.ps1 rewrites it for a new ngrok tunnel."""
    last_mtime: float | None = None
    while True:
        await asyncio.sleep(interval)
        try:
            mtime = os.stat(env_file).st_mtime
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            changed = refresh_public_base_url(settings, env_file)
            if changed:
                log.info("PUBLIC_BASE_URL changed in backend/.env - now using %s", changed)
        except Exception:
            log.exception("Could not re-read PUBLIC_BASE_URL")


async def _reminder_loop(app: FastAPI) -> None:
    """Every few minutes in the evening call window, ring patients about tomorrow's appointments."""
    await asyncio.sleep(60)
    while True:
        try:
            client = elevenlabs_client(app)
            if client is not None and app.state.telephony.enabled:
                await run_due_reminders(get_sessionmaker(), client)
        except Exception:
            log.exception("Reminder call loop failed")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=90.0),
    )
    app.state.calendar = build_calendar_gateway()
    app.state.telephony = build_telephony(settings, twiml_app_sid=load_state().get("twiml_app_sid"))
    app.state.event_bus = SessionEventBus(settings.asyncpg_dsn)
    await app.state.event_bus.start()
    app.state.vertex_tokens = vertex_proxy.VertexTokenProvider(settings.vertex_credentials_file)
    app.state.thought_signatures = vertex_proxy.ThoughtSignatureCache()

    background: list[asyncio.Task[None]] = []
    if app.state.calendar.enabled:
        background.append(
            asyncio.create_task(_calendar_resync_loop(app, settings.calendar_resync_interval_seconds), name="calendar-resync")
        )
    if settings.reminder_calls_enabled:
        background.append(asyncio.create_task(_reminder_loop(app), name="reminder-calls"))
    env_file = settings.model_config.get("env_file")
    if settings.environment == "development" and isinstance(env_file, (str, Path)) and os.path.exists(env_file):
        background.append(asyncio.create_task(_public_url_watch_loop(settings, str(env_file)), name="public-url-watch"))
    log.info(
        "%s assistant API ready (calendar=%s, telephony=%s, public URL=%s)",
        settings.clinic_name,
        "google" if app.state.calendar.enabled else "disabled",
        "twilio" if app.state.telephony.enabled else "disabled",
        settings.public_base_url,
    )
    weak = settings.placeholder_secrets()
    if weak:
        log.warning(
            "%s still set to a placeholder - anyone who can reach %s could use it. Run: python -m app.cli rotate-secrets",
            ", ".join(name.upper() for name in weak),
            settings.public_base_url,
        )
    try:
        yield
    finally:
        for task in background:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.event_bus.stop()
        await app.state.http.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.clinic_name} - AI receptionist API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/tools/start_emergency_call"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_input",
                    "message": tools.EMERGENCY_FALLBACK,
                }
            )
        if request.url.path.startswith("/tools/"):
            # Give the agent something it can act on instead of a bare 422.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err.get('loc', [])[1:]) or 'body'}: {err.get('msg')}" for err in exc.errors()
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_input",
                    "message": f"Some details were missing or invalid ({problems}). Ask the patient to repeat them, then try again.",
                }
            )
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        try:
            async with get_sessionmaker()() as db:
                await db.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"status": "unavailable", "database": str(exc)[:200]}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    app.include_router(clinic.router)
    app.include_router(sessions.router)
    app.include_router(sessions.sos_router)
    app.include_router(tools.router)
    app.include_router(vertex_proxy.router)
    app.include_router(webhooks.router)
    app.include_router(twilio_webhooks.router)
    app.include_router(admin.router)

    if Path(FRONTEND_DIST, "index.html").exists():
        # Serve the built React app from the same origin (production-style).
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


app = create_app()
