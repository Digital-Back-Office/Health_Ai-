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
