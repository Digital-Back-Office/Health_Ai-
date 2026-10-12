import json

import httpx
import pytest
import respx

from app.config import get_settings
from app.llm.vertex_proxy import (
    DUMMY_THOUGHT_SIGNATURE,
    SignatureCollector,
    ThoughtSignatureCache,
    prepare_payload,
)

VERTEX_URL = (
    "https://europe-west2-aiplatform.googleapis.com/v1/projects/test-project/locations/europe-west2/endpoints/openapi/chat/completions"
)
AUTH = {"Authorization": "Bearer test-llm-secret"}


def _sse(*chunks: dict, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


class FakeTokens:
    def __init__(self) -> None:
        self.refreshes = 0

    async def auth_headers(self, force_refresh: bool = False) -> dict[str, str]:
        self.refreshes += int(force_refresh)
        return {"authorization": "Bearer google-access-token", "x-goog-user-project": "test-quota-project"}


@pytest.fixture(autouse=True)
def _fake_google_tokens(client: httpx.AsyncClient):
    from app.main import app

    app.state.vertex_tokens = FakeTokens()
    yield


async def test_prepare_payload_adapts_elevenlabs_request() -> None:
    cache = ThoughtSignatureCache()
    cache.put("call_known", "sig-known")
    body = {
        "model": "whatever",
        "messages": [
            {"role": "system", "content": "You are Sophie"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_known", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                    {"id": "call_unknown", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_known", "content": "{}"},
        ],
        "temperature": 0.3,
        "max_tokens": -1,
        "stream": True,
        "user_id": "abc",
        "elevenlabs_extra_body": {"x": 1},
    }
    payload = prepare_payload(body, get_settings(), cache)
    assert payload["model"] == "google/gemini-3.5-flash"
    assert "temperature" not in payload and "max_tokens" not in payload
    assert "user_id" not in payload and "elevenlabs_extra_body" not in payload
    assert payload["reasoning_effort"] == "minimal"
    calls = payload["messages"][1]["tool_calls"]
    assert calls[0]["extra_content"]["google"]["thought_signature"] == "sig-known"
    assert calls[1]["extra_content"]["google"]["thought_signature"] == DUMMY_THOUGHT_SIGNATURE


async def test_signature_collector_handles_split_deltas() -> None:
    cache = ThoughtSignatureCache()
    collector = SignatureCollector(cache)
    collector.feed({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "x"}}]}}]})
    collector.feed(
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "extra_content": {"google": {"thought_signature": "s1"}}}]}}]}
    )
    assert cache.get("call_1") == "s1" and collector.tool_calls == 1


async def test_proxy_rejects_bad_credentials(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json={"messages": []}, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


@respx.mock
async def test_proxy_streams_and_preserves_thought_signatures(client: httpx.AsyncClient) -> None:
    tool_chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_42",
                            "type": "function",
                            "function": {"name": "check_availability", "arguments": "{\"service_code\": \"gp_consultation\"}"},
                            "extra_content": {"google": {"thought_signature": "real-signature"}},
                        }
                    ]
                },
            }
        ]
    }
    route = respx.post(VERTEX_URL).mock(
        side_effect=[
            httpx.Response(200, content=_sse(tool_chunk), headers={"content-type": "text/event-stream"}),
            httpx.Response(200, content=_sse({"choices": [{"index": 0, "delta": {"content": "Hello"}}]}), headers={"content-type": "text/event-stream"}),
        ]
    )

    first = await client.post(
        "/v1/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "GP please"}]}, headers=AUTH
    )
    assert first.status_code == 200
    lines = [line for line in first.text.split("\n") if line.startswith("data:")]
    assert json.loads(lines[0][5:])["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_42"
    assert lines[-1] == "data: [DONE]"
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "google/gemini-3.5-flash"
    assert route.calls[0].request.headers["authorization"] == "Bearer google-access-token"
    assert route.calls[0].request.headers["x-goog-user-project"] == "test-quota-project"

    # ElevenLabs sends the history back WITHOUT the signature - the proxy restores it.
    follow_up = {
        "stream": True,
        "messages": [
            {"role": "user", "content": "GP please"},
            {"role": "assistant", "tool_calls": [{"id": "call_42", "type": "function", "function": {"name": "check_availability", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_42", "content": "{\"ok\": true}"},
        ],
    }
    second = await client.post("/v1", json=follow_up, headers=AUTH)  # base-URL style path works too
    assert second.status_code == 200 and "Hello" in second.text
    resent = json.loads(route.calls[1].request.content)
    assert resent["messages"][1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "real-signature"


@respx.mock
async def test_proxy_retries_without_reasoning_effort_when_rejected(client: httpx.AsyncClient) -> None:
    route = respx.post(VERTEX_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "Invalid value for reasoning_effort"}}),
            httpx.Response(200, content=_sse({"choices": [{"index": 0, "delta": {"content": "Hi"}}]}), headers={"content-type": "text/event-stream"}),
        ]
    )
    response = await client.post("/v1/chat/completions", json={"stream": True, "messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
    assert response.status_code == 200 and "Hi" in response.text
    assert "reasoning_effort" in json.loads(route.calls[0].request.content)
    assert "reasoning_effort" not in json.loads(route.calls[1].request.content)


@respx.mock
async def test_proxy_surfaces_persistent_upstream_errors(client: httpx.AsyncClient) -> None:
    respx.post(VERTEX_URL).mock(return_value=httpx.Response(503, text="overloaded"))
    response = await client.post("/v1/chat/completions", json={"stream": True, "messages": []}, headers=AUTH)
    assert response.status_code == 502


@respx.mock
async def test_proxy_non_streaming(client: httpx.AsyncClient) -> None:
    respx.post(VERTEX_URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}))
    response = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
    assert response.json()["choices"][0]["message"]["content"] == "Hi"
