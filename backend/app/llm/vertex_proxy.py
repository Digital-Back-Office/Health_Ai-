"""OpenAI-compatible Chat Completions proxy in front of Vertex AI (Gemini).

    ElevenLabs agent (Custom LLM)
        --POST /v1/chat/completions, Authorization: Bearer <LLM_PROXY_SECRET>-->
    this proxy
        --Vertex AI OpenAI-compatible endpoint, short-lived Google OAuth token-->
    Gemini

Why a proxy rather than pointing ElevenLabs at Vertex directly: Vertex wants a
Google OAuth access token that expires hourly, not a static API key. The proxy
also keeps Gemini 3 "thought signatures" intact across tool-calling turns and
pins the model and region (VERTEX_MODEL / VERTEX_LOCATION).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["llm-proxy"])

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# Documented by Google for function calls whose real signature is unavailable.
DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
DROPPED_REQUEST_KEYS = {"elevenlabs_extra_body", "user_id", "user"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class VertexAuthError(Exception):
    """Google credentials can't be used, e.g. the gcloud login has expired."""


class VertexTokenProvider:
    """Google credentials for Vertex AI, refreshed before expiry.

    Uses a service-account key when VERTEX_CREDENTIALS_FILE is set, otherwise
    Application Default Credentials (`gcloud auth application-default login`
    locally, or the attached service account when deployed on Google Cloud).
    """

    def __init__(self, credentials_file: Path | None) -> None:
        self._credentials_file = credentials_file
        self._credentials: Any = None
        self._lock = asyncio.Lock()

    def _load(self) -> Any:
        if self._credentials_file:
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(self._credentials_file), scopes=[CLOUD_PLATFORM_SCOPE]
            )
        import google.auth

        credentials, _project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        return credentials

    async def auth_headers(self, force_refresh: bool = False) -> dict[str, str]:
        """Authorization header, plus x-goog-user-project when the credentials carry
        a quota project (as gcloud user credentials usually do)."""
        from google.auth.exceptions import GoogleAuthError

        async with self._lock:
            try:
                if self._credentials is None:
                    self._credentials = await asyncio.to_thread(self._load)
                if force_refresh or not self._credentials.valid:
                    from google.auth.transport.requests import Request as GoogleRequest

                    await asyncio.to_thread(self._credentials.refresh, GoogleRequest())
            except GoogleAuthError as exc:
                # Load them again next time, so a fresh `gcloud auth application-default login`
                # is picked up without restarting the backend.
                self._credentials = None
                raise VertexAuthError(str(exc)) from exc
            headers: dict[str, str] = {}
            self._credentials.apply(headers)
            return headers

    def describe(self) -> str:
        """Human-readable identity, for diagnostics (no secrets)."""
        credentials = self._credentials
        if credentials is None:
            return "not loaded yet"
        email = getattr(credentials, "service_account_email", None)
        kind = f"service account {email}" if email else f"{type(credentials).__name__} (application default credentials)"
        quota = getattr(credentials, "quota_project_id", None)
        return f"{kind}{f', quota project {quota}' if quota else ''}"


class ThoughtSignatureCache:
    """tool_call_id -> Gemini thought signature (bounded, time-limited)."""

    def __init__(self, max_items: int = 5000, ttl_seconds: float = 3600) -> None:
        self._items: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max_items = max_items
        self._ttl = ttl_seconds

    def put(self, tool_call_id: str, signature: str) -> None:
        self._items[tool_call_id] = (signature, time.monotonic())
        self._items.move_to_end(tool_call_id)
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)

    def get(self, tool_call_id: str | None) -> str | None:
        if not tool_call_id or tool_call_id not in self._items:
            return None
        signature, stored_at = self._items[tool_call_id]
        if time.monotonic() - stored_at > self._ttl:
            del self._items[tool_call_id]
            return None
        return signature


_warm_ups: set[asyncio.Task[None]] = set()


async def _warm_up(http: httpx.AsyncClient, tokens: VertexTokenProvider, settings: Settings) -> None:
    try:
        await tokens.auth_headers()  # refresh the Google token now if it's close to expiry
        host = settings.vertex_base_url.split("/v1/")[0]
        await http.get(f"{host}/", timeout=5.0)  # open a pooled TLS connection to Vertex
    except Exception as exc:  # best effort only
        log.debug("Vertex warm-up skipped: %s", exc)


def schedule_warm_up(app_state: Any) -> None:
    """Called when a conversation starts: while the agent says its greeting, get
    the Google token and the connection to Vertex ready for the first real turn."""
    settings = get_settings()
    if not settings.vertex_project_id or settings.environment == "test":
        return
    task = asyncio.create_task(_warm_up(app_state.http, app_state.vertex_tokens, settings))
    _warm_ups.add(task)
    task.add_done_callback(_warm_ups.discard)


def _is_gemini_3(model: str) -> bool:
    return "gemini-3" in model


def prepare_payload(body: dict[str, Any], settings: Settings, signatures: ThoughtSignatureCache) -> dict[str, Any]:
    """Adapt an incoming OpenAI-style request for Vertex AI."""
    payload = {key: value for key, value in body.items() if key not in DROPPED_REQUEST_KEYS}
    model = settings.vertex_model
    payload["model"] = model

    for key in ("max_tokens", "max_completion_tokens"):
        if key in payload and (payload[key] is None or payload[key] <= 0):
            del payload[key]
    if settings.vertex_drop_temperature and _is_gemini_3(model):
        payload.pop("temperature", None)
    if settings.vertex_reasoning_effort and "reasoning_effort" not in payload:
        payload["reasoning_effort"] = settings.vertex_reasoning_effort

    # Gemini 3 rejects function-call history without its thought signatures.
    # Re-attach the ones we saw on the way out; fall back to Google's dummy value.
    for message in payload.get("messages") or []:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        for call in message["tool_calls"]:
            extra = call.get("extra_content") or {}
            google = extra.get("google") or {}
            if google.get("thought_signature"):
                continue
            signature = signatures.get(call.get("id")) or (DUMMY_THOUGHT_SIGNATURE if _is_gemini_3(model) else None)
            if signature:
                call["extra_content"] = {**extra, "google": {**google, "thought_signature": signature}}
    return payload


class SignatureCollector:
    """Picks thought signatures out of streamed tool-call deltas."""

    def __init__(self, signatures: ThoughtSignatureCache) -> None:
        self._signatures = signatures
        self._ids: dict[tuple[int, int], str] = {}
        self._pending: dict[tuple[int, int], str] = {}
        self.tool_calls = 0

    def feed(self, chunk: dict[str, Any]) -> None:
        for choice in chunk.get("choices") or []:
            choice_index = choice.get("index", 0)
            message = choice.get("delta") or choice.get("message") or {}
            for call in message.get("tool_calls") or []:
                key = (choice_index, call.get("index", 0))
                if call.get("id") and key not in self._ids:
                    self._ids[key] = call["id"]
                    self.tool_calls += 1
                signature = ((call.get("extra_content") or {}).get("google") or {}).get("thought_signature")
                if signature:
                    self._pending[key] = signature
                if key in self._ids and key in self._pending:
                    self._signatures.put(self._ids[key], self._pending.pop(key))


def _authorise(request: Request, settings: Settings) -> None:
    expected = settings.llm_proxy_secret.get_secret_value()
    header = request.headers.get("authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-proxy-secret", "")
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid proxy credentials")


async def _send(
    http: httpx.AsyncClient, tokens: VertexTokenProvider, settings: Settings, payload: dict[str, Any], *, stream: bool
) -> httpx.Response:
    """POST to Vertex with retries for transient errors, token expiry and thinking-param rejections."""
    url = f"{settings.vertex_base_url}/chat/completions"
    force_refresh = False
    for attempt in range(3):
        try:
            headers = await tokens.auth_headers(force_refresh=force_refresh)
        except VertexAuthError as exc:
            log.error(
                "Google credentials for Vertex AI don't work: %s  Fix: run `gcloud auth application-default login` "
                "(or set VERTEX_CREDENTIALS_FILE); no restart needed.",
                exc,
            )
            raise HTTPException(status_code=503, detail="Vertex AI credentials need attention - see the backend log") from exc
        request = http.build_request(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(settings.vertex_timeout_seconds, connect=5.0),
        )
        try:
            response = await http.send(request, stream=stream)
        except httpx.TransportError as exc:  # timeouts, dropped connections
            log.warning("Vertex request failed (attempt %d): %s", attempt + 1, type(exc).__name__)
            if attempt < 1:
                continue
            raise HTTPException(status_code=504, detail="Vertex AI didn't answer in time") from exc
        if response.status_code == 200:
            return response
        body = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        log.warning("Vertex returned %s (attempt %d): %s", response.status_code, attempt + 1, body[:500])
        if response.status_code == 401 and not force_refresh:
            force_refresh = True
            continue
        if response.status_code == 400 and "reasoning_effort" in payload and (
            "reasoning" in body.lower() or "thinking" in body.lower()
        ):
            payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}
            continue
        if response.status_code in RETRYABLE_STATUS and attempt < 2:
            await asyncio.sleep(0.3 * (attempt + 1))
            continue
        raise HTTPException(status_code=502, detail=f"Vertex AI error {response.status_code}: {body[:300]}")
    raise HTTPException(status_code=502, detail="Vertex AI request failed after retries")


async def _relay(
    response: httpx.Response, collector: SignatureCollector, request_id: str, started: float
) -> AsyncIterator[str]:
    first_token_at: float | None = None
    saw_done = False
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue  # blank separators, comments, other SSE fields
            data = line[5:].strip()
            if data == "[DONE]":
                saw_done = True
                yield "data: [DONE]\n\n"
                continue
            if first_token_at is None:
                first_token_at = time.perf_counter()
            try:
                collector.feed(json.loads(data))
            except json.JSONDecodeError:
                pass
            yield f"data: {data}\n\n"
    except httpx.HTTPError as exc:
        log.warning("[%s] Vertex stream interrupted: %s", request_id, exc)
    finally:
        await response.aclose()
    if not saw_done:
        yield "data: [DONE]\n\n"
    ttft = (first_token_at - started) * 1000 if first_token_at else -1
    log.info(
        "[%s] LLM stream done: first token %.0f ms, total %.0f ms, tool calls %d",
        request_id,
        ttft,
        (time.perf_counter() - started) * 1000,
        collector.tool_calls,
    )


@router.post("/v1/chat/completions")
@router.post("/v1")
async def chat_completions(request: Request) -> Any:
    settings = get_settings()
    _authorise(request, settings)
    if not settings.vertex_project_id:
        raise HTTPException(status_code=503, detail="VERTEX_PROJECT_ID is not configured")
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON") from exc

    state = request.app.state
    signatures: ThoughtSignatureCache = state.thought_signatures
    payload = prepare_payload(body, settings, signatures)
    wants_stream = bool(body.get("stream", False))
    payload["stream"] = wants_stream
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    log.info(
        "[%s] LLM request: model=%s messages=%d tools=%d stream=%s",
        request_id,
        payload["model"],
        len(payload.get("messages") or []),
        len(payload.get("tools") or []),
        wants_stream,
    )

    response = await _send(state.http, state.vertex_tokens, settings, payload, stream=wants_stream)
    collector = SignatureCollector(signatures)
    if not wants_stream:
        data = response.json()
        collector.feed(data)
        return JSONResponse(data)
    return StreamingResponse(
        _relay(response, collector, request_id, started),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
