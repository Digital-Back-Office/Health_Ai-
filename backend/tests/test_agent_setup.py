from datetime import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.setup import (
    ClinicSnapshot,
    agent_payload,
    build_prompt,
    describe_weekly_hours,
    tool_configs,
)
from app.config import get_settings
from app.models import Clinician, Service


async def _snapshot(db: AsyncSession) -> ClinicSnapshot:
    services = list((await db.execute(select(Service).options(selectinload(Service.clinicians)).order_by(Service.display_order))).scalars())
    clinicians = list((await db.execute(select(Clinician).order_by(Clinician.full_name))).scalars())
    return ClinicSnapshot(services, clinicians)


def test_describe_weekly_hours_compresses_ranges() -> None:
    windows = {d: [(time(8, 30), time(13)), (time(14), time(18, 30))] for d in range(5)}
    assert describe_weekly_hours(windows) == "Monday to Friday: 8:30am to 1pm and 2pm to 6:30pm"


async def test_prompt_has_all_clinic_facts(db: AsyncSession) -> None:
    prompt = build_prompt(get_settings(), await _snapshot(db))
    assert "[[" not in prompt
    assert "{{system__time}}" in prompt  # filled in by ElevenLabs at runtime
    assert "[gp_consultation]" in prompt and "£95" in prompt
    assert "Dr Sarah Patel [1]" in prompt
    assert "999" in prompt and "NHS 111" in prompt


async def test_tool_configs(db: AsyncSession) -> None:
    clinic = await _snapshot(db)
    tools = tool_configs(get_settings(), clinic, "secret-id", "header_variable")
    names = [t["name"] for t in tools]
    assert names == [
        "verify_patient",
        "register_patient",
        "check_availability",
        "book_appointment",
        "get_my_appointments",
        "reschedule_appointment",
        "cancel_appointment",
        "request_callback",
        "start_emergency_call",
        "connect_to_reception",
    ]
    for tool in tools:
        schema = tool["api_schema"]
        assert schema["url"].endswith(f"/tools/{tool['name']}") and schema["method"] == "POST"
        assert schema["request_headers"]["X-Tool-Secret"] == {"secret_id": "secret-id"}
        assert schema["request_headers"]["X-Session-Token"] == {"variable_name": "secret__session_token"}
        body = schema.get("request_body_schema")
        if body:
            assert set(body["required"]) <= set(body["properties"])
            assert "session_token" not in body["properties"]  # never exposed to the LLM
    availability = next(t for t in tools if t["name"] == "check_availability")
    assert "gp_consultation" in availability["api_schema"]["request_body_schema"]["properties"]["service_code"]["enum"]

    body_mode = tool_configs(get_settings(), clinic, "secret-id", "body_variable")
    token_property = body_mode[0]["api_schema"]["request_body_schema"]["properties"]["session_token"]
    assert token_property["dynamic_variable"] == "session_token"


async def test_agent_payload_uses_custom_llm_and_auth(db: AsyncSession) -> None:
    clinic = await _snapshot(db)
    payload = agent_payload(
        get_settings(),
        clinic,
        prompt="p",
        tool_ids=["t1"],
        documents=[],
        llm_secret_id="llm-secret",
        voice_id="voice",
        token_var="secret__session_token",
    )
    prompt_cfg = payload["conversation_config"]["agent"]["prompt"]
    assert prompt_cfg["llm"] == "custom-llm"
    assert prompt_cfg["custom_llm"]["api_key"] == {"secret_id": "llm-secret"}
    assert prompt_cfg["custom_llm"]["url"].endswith("/v1")
    assert prompt_cfg["timezone"] == "Europe/London"
    assert payload["conversation_config"]["agent"]["language"] == "en"
    assert payload["platform_settings"]["auth"]["enable_auth"] is True


class _FakeVoiceApi:
    """Just enough of the ElevenLabs admin API for resolve_voice."""

    def __init__(self, account: set[str], library: dict[str, str]) -> None:
        self.account, self.library, self.added = account, library, []

    def call(self, method: str, path: str, *, json=None, params=None, allow_404: bool = False):
        from app.agent.setup import ApiError

        if method == "GET" and path.startswith("/v1/voices/"):
            if path.rsplit("/", 1)[1] not in self.account:
                raise ApiError(method, path, 400, '{"detail":{"status":"voice_not_found"}}')
            return {}
        if path == "/v1/shared-voices":
            return {"voices": [{"voice_id": vid, "public_owner_id": owner, "name": "Sally"} for vid, owner in self.library.items() if vid in params["search"]]}
        if method == "POST" and path.startswith("/v1/voices/add/"):
            voice_id = path.rsplit("/", 1)[1]
            self.added.append(voice_id)
            self.account.add(voice_id)
            return {"voice_id": voice_id}
        raise AssertionError(f"unexpected call {method} {path}")


def test_voice_library_voice_is_added_and_a_bad_id_keeps_the_current_voice(monkeypatch) -> None:
    from app.agent.setup import resolve_voice

    settings = get_settings()
    state = {"voice_id": "alice"}
    api = _FakeVoiceApi(account={"alice"}, library={"sally": "owner-1"})

    monkeypatch.setattr(settings, "elevenlabs_voice_id", "sally")
    assert resolve_voice(api, settings, state, print) == "sally" and api.added == ["sally"]

    monkeypatch.setattr(settings, "elevenlabs_voice_id", "typo-id")
    assert resolve_voice(api, settings, state, print) == "alice"


def test_secret_creation_retries_elevenlabs_500s_with_a_neutral_name(monkeypatch) -> None:
    from app.agent import setup
    from app.agent.setup import ApiError, ensure_secret

    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    names: list[str] = []

    class Api:
        def call(self, method, path, *, json=None, params=None, allow_404=False):
            names.append(json["name"])
            if "tool_secret" in json["name"]:
                raise ApiError(method, path, 500, '{"status": "internal_server_error"}')
            return {"secret_id": "sec_1"}

    state: dict = {}
    assert ensure_secret(Api(), state, "hc_tool_secret", "value") == ("sec_1", None)
    assert len(names) == 3 and names[-1].startswith("healthai_")
    assert state["secrets"]["hc_tool_secret"]["id"] == "sec_1"


async def test_dental_consultation_is_offered_by_the_dentist(db: AsyncSession) -> None:
    clinic = await _snapshot(db)
    dental = next(s for s in clinic.services if s.code == "dental_consultation")
    assert dental.price_pence == 7500 and dental.duration_minutes == 30
    assert [c.full_name for c in dental.clinicians] == ["Dr Priya Shah"]
    assert "[dental_consultation]" in build_prompt(get_settings(), clinic)
