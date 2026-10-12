"""Minimal ElevenLabs API client for starting authenticated conversations."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from ..config import BACKEND_DIR, get_settings

log = logging.getLogger(__name__)

STATE_FILE = BACKEND_DIR / ".elevenlabs-state.json"


class ElevenLabsError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    """Ids written by `python -m app.cli setup-agent` (agent, tools, documents)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


DEFAULT_SESSION_TOKEN_VAR = "secret__session_token"


def resolve_agent_id() -> str | None:
    return get_settings().elevenlabs_agent_id or load_state().get("agent_id")


def session_token_var() -> str:
    """Name of the dynamic variable that carries our session token to tools."""
    return (
        get_settings().elevenlabs_session_token_var
        or load_state().get("session_token_var")
        or DEFAULT_SESSION_TOKEN_VAR
    )


class ElevenLabsClient:
    def __init__(self, http: httpx.AsyncClient, *, api_key: str, agent_id: str, base_url: str) -> None:
        self._http = http
        self._headers = {"xi-api-key": api_key}
        self.agent_id = agent_id
        self._base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.get(
                f"{self._base_url}{path}", params=params, headers=self._headers, timeout=10.0
            )
        except httpx.HTTPError as exc:
            raise ElevenLabsError(f"Could not reach ElevenLabs: {exc}") from exc
        if response.status_code != 200:
            detail = response.text[:300]
            log.warning("ElevenLabs %s returned %s: %s", path, response.status_code, detail)
            raise ElevenLabsError(f"ElevenLabs returned {response.status_code}: {detail}", response.status_code)
        return response.json()

    async def conversation_token(self) -> str:
        """Short-lived token for a WebRTC (voice) conversation."""
        data = await self._get("/v1/convai/conversation/token", {"agent_id": self.agent_id})
        return data["token"]

    async def signed_url(self) -> str:
        """Signed WebSocket URL (used for text-only chat); valid for 15 minutes."""
        data = await self._get("/v1/convai/conversation/get-signed-url", {"agent_id": self.agent_id})
        return data["signed_url"]
