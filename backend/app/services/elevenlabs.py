"""Minimal ElevenLabs API client for starting authenticated conversations."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from ..config import BACKEND_DIR, get_settings

log = logging.getLogger(__name__)

# Written by setup-agent, read at runtime. Docker keeps it on a volume (ELEVENLABS_STATE_FILE=/data/...).
STATE_FILE = Path(os.environ.get("ELEVENLABS_STATE_FILE") or BACKEND_DIR / ".elevenlabs-state.json")


class ElevenLabsError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def load_state(path: Path | None = None) -> dict[str, Any]:
    """Ids written by `python -m app.cli setup-agent` (agent, tools, documents, phone number)."""
    try:
        return json.loads((path or STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    (path or STATE_FILE).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


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

    async def outbound_call(
        self,
        *,
        to_number: str,
        phone_number_id: str,
        dynamic_variables: dict[str, str],
        first_message: str | None = None,
        ringing_timeout_secs: int = 40,
    ) -> dict[str, Any]:
        """Ring a patient from the clinic's Twilio number with this agent (reminder calls)."""
        client_data: dict[str, Any] = {"dynamic_variables": dynamic_variables}
        if first_message:
            client_data["conversation_config_override"] = {"agent": {"first_message": first_message}}
        body = {
            "agent_id": self.agent_id,
            "agent_phone_number_id": phone_number_id,
            "to_number": to_number,
            "conversation_initiation_client_data": client_data,
            "telephony_call_config": {"ringing_timeout_secs": ringing_timeout_secs},
        }
        try:
            response = await self._http.post(
                f"{self._base_url}/v1/convai/twilio/outbound-call", json=body, headers=self._headers, timeout=20.0
            )
        except httpx.HTTPError as exc:
            raise ElevenLabsError(f"Could not reach ElevenLabs: {exc}") from exc
        if response.status_code != 200:
            raise ElevenLabsError(f"ElevenLabs returned {response.status_code}: {response.text[:300]}", response.status_code)
        data = response.json()
        if not data.get("success", False):
            raise ElevenLabsError(f"Outbound call not placed: {data.get('message')}")
        return data
