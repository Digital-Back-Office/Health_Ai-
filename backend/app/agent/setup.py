"""Create or update the ElevenLabs agent: secrets, tools, knowledge base, prompt, voice, LLM.

Run with `python -m app.cli setup-agent`. Idempotent: the ids of everything it
creates are kept in backend/.elevenlabs-state.json, so re-running updates in
place (re-run it whenever PUBLIC_BASE_URL, services, prices or prompts change).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..services.clinic_hours import opening_hours
from ..config import Settings, get_settings
from ..db import dispose_engine, get_sessionmaker
from ..models import Clinician, Service
from ..services.elevenlabs import DEFAULT_SESSION_TOKEN_VAR, load_state, save_state
from ..timeutil import format_price

AGENT_DIR = Path(__file__).resolve().parent
KB_DIR = AGENT_DIR / "knowledge_base"
PROMPT_TEMPLATE = AGENT_DIR / "system_prompt.md"

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class SetupError(RuntimeError):
    pass


class ApiError(SetupError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} failed with HTTP {status}:\n{body[:2000]}")
        self.status = status
        self.body = body

    @property
    def message(self) -> str:
        """ElevenLabs' human-readable error message, if the body has one."""
        try:
            detail = json.loads(self.body).get("detail")
        except (ValueError, AttributeError):
            return self.body[:300]
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)[:300]
        return str(detail)[:300]


class ElevenLabsAdmin:
    def __init__(self, api_key: str, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers={"xi-api-key": api_key}, timeout=60.0)

    def call(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None, allow_404: bool = False) -> Any:
        response = self._client.request(method, path, json=json, params=params)
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            raise ApiError(method, path, response.status_code, response.text)
        return response.json() if response.content else {}

    def close(self) -> None:
        self._client.close()


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# --- clinic facts for the prompt ---------------------------------------------------


@dataclass
class ClinicSnapshot:
    services: list[Service]
    clinicians: list[Clinician]


async def load_clinic() -> ClinicSnapshot:
    try:
        async with get_sessionmaker()() as db:
            services = list(
                (
                    await db.execute(
                        select(Service)
                        .options(selectinload(Service.clinicians))
                        .where(Service.is_active.is_(True))
                        .order_by(Service.display_order)
                    )
                ).scalars()
            )
            clinicians = list(
                (await db.execute(select(Clinician).where(Clinician.is_active.is_(True)).order_by(Clinician.full_name))).scalars()
            )
            return ClinicSnapshot(services, clinicians)
    finally:
        await dispose_engine()


def _clock(value: dtime) -> str:
    hour12 = value.hour % 12 or 12
    suffix = "am" if value.hour < 12 else "pm"
    return f"{hour12}{suffix}" if value.minute == 0 else f"{hour12}:{value.minute:02d}{suffix}"


def describe_weekly_hours(windows_by_day: dict[int, list[tuple[dtime, dtime]]]) -> str:
    """Compress e.g. Mon-Fri with identical windows into 'Monday to Friday: ...'."""
    lines: list[str] = []
    day = 0
    while day < 7:
        windows = windows_by_day.get(day, [])
        end = day
        while end + 1 < 7 and windows_by_day.get(end + 1, []) == windows:
            end += 1
        label = DAY_NAMES[day] if day == end else f"{DAY_NAMES[day]} to {DAY_NAMES[end]}"
        text = " and ".join(f"{_clock(a)} to {_clock(b)}" for a, b in windows) if windows else "not working"
        lines.append(f"{label}: {text}")
        day = end + 1
    return "; ".join(line for line in lines if not line.endswith("not working")) or "no regular hours"


def build_prompt(settings: Settings, clinic: ClinicSnapshot) -> str:
    hours = opening_hours(clinic.clinicians)
    hours_text = "\n".join(
        f"- {d['day']}: "
        + (f"{_clock(dtime.fromisoformat(d['open']))} to {_clock(dtime.fromisoformat(d['close']))}" if d["open"] else "closed")
        for d in hours
    )
    services_text = "\n".join(
        f"- {s.name} [{s.code}]: {s.duration_minutes} minutes, {format_price(s.price_pence)}. "
        f"With: {', '.join(c.full_name for c in s.clinicians if c.is_active) or 'nobody currently'}. {s.description}"
        for s in clinic.services
    )
    clinician_lines = []
    for c in clinic.clinicians:
        by_day: dict[int, list[tuple[dtime, dtime]]] = {}
        for w in sorted(c.working_hours, key=lambda w: (w.weekday, w.start_time)):
            by_day.setdefault(w.weekday, []).append((w.start_time, w.end_time))
        services = ", ".join(s.name for s in c.services if s.is_active)
        clinician_lines.append(
            f"- {c.full_name} [{c.id}], {c.role}. Offers: {services}. Usually works {describe_weekly_hours(by_day)}."
        )
    replacements = {
        "assistant_name": settings.assistant_name,
        "clinic_name": settings.clinic_name,
        "clinic_address": settings.clinic_address,
        "clinic_phone": settings.clinic_phone,
        "clinic_email": settings.clinic_email,
        "opening_hours": hours_text,
        "services": services_text,
        "clinicians": "\n".join(clinician_lines),
    }
    prompt = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    for key, value in replacements.items():
        prompt = prompt.replace(f"[[{key}]]", value)
    return prompt


# --- tools ----------------------------------------------------------------------------

# How the per-session token reaches our webhooks. Tried in order; the first
# configuration ElevenLabs accepts is remembered in the state file.
TOKEN_TRANSPORTS = ("header_variable", "header_template", "body_variable")


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def tool_configs(
    settings: Settings, clinic: ClinicSnapshot, tool_secret_id: str, transport: str
) -> list[dict[str, Any]]:
    base = settings.public_base_url
    token_var = DEFAULT_SESSION_TOKEN_VAR if transport != "body_variable" else "session_token"
    service_codes = [s.code for s in clinic.services]
    clinician_ids = ", ".join(f"{c.id} = {c.full_name}" for c in clinic.clinicians)

    def webhook(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str],
        *,
        timeout: int = 20,
    ) -> dict[str, Any]:
        headers: dict[str, Any] = {"X-Tool-Secret": {"secret_id": tool_secret_id}}
        body_properties = dict(properties)
        if transport == "header_variable":
            headers["X-Session-Token"] = {"variable_name": token_var}
        elif transport == "header_template":
            headers["X-Session-Token"] = "{{" + token_var + "}}"
        else:
            body_properties["session_token"] = {"type": "string", "description": "", "dynamic_variable": token_var}
        schema: dict[str, Any] = {
            "url": f"{base}/tools/{name}",
            "method": "POST",
            "request_headers": headers,
            "content_type": "application/json",
        }
        if body_properties:
            schema["request_body_schema"] = {
                "type": "object",
                "description": f"Arguments for {name}.",
                "properties": body_properties,
                "required": required,
            }
        return {
            "type": "webhook",
            "name": name,
            "description": description,
            "response_timeout_secs": timeout,
            "api_schema": schema,
        }

    date_hint = "Format YYYY-MM-DD."
    return [
        webhook(
            "verify_patient",
            "Confirm a returning patient's identity. Needs their surname, date of birth and the phone number "
            "they registered with. Must succeed before discussing or changing their appointments. Returns their "
            "upcoming appointments.",
            {
                "last_name": _string("The patient's surname, spelled as they confirmed it."),
                "date_of_birth": _string(f"The patient's date of birth. {date_hint}"),
                "phone": _string(
                    "The phone number the patient registered with, e.g. 07700 900123. Leave empty on a phone call "
                    "when caller_known is yes (their number is already known)."
                ),
            },
            ["last_name", "date_of_birth"],
        ),
        webhook(
            "register_patient",
            "Register a new patient (first visit) so they can book. If they already have a record they are "
            "verified instead.",
            {
                "first_name": _string("The patient's first name."),
                "last_name": _string("The patient's surname."),
                "date_of_birth": _string(f"The patient's date of birth. {date_hint}"),
                "phone": _string("The patient's UK mobile or phone number."),
                "email": _string("The patient's email address, if they gave one. Empty string if not."),
            },
            ["first_name", "last_name", "date_of_birth", "phone"],
        ),
        webhook(
            "check_availability",
            "Find free appointment slots for a service. Use before offering any times. Returns up to six "
            "options with clinician_id and start_time to use when booking.",
            {
                "service_code": {"type": "string", "enum": service_codes, "description": "Which service the patient needs."},
                "clinician_id": {
                    "type": "integer",
                    "description": f"Only if the patient asked for a specific clinician ({clinician_ids}). Omit otherwise.",
                },
                "date_from": _string(f"First day to search. {date_hint} Omit to start from today."),
                "date_to": _string(f"Last day to search. {date_hint} Omit for the next two weeks."),
                "part_of_day": {
                    "type": "string",
                    "enum": ["any", "morning", "afternoon", "evening"],
                    "description": "The patient's preferred time of day, or any.",
                },
            },
            ["service_code"],
        ),
        webhook(
            "book_appointment",
            "Book an appointment slot returned by check_availability, only after the patient has clearly said yes "
            "to the read-back. The patient must be verified or registered first.",
            {
                "service_code": {"type": "string", "enum": service_codes, "description": "The service being booked."},
                "clinician_id": {"type": "integer", "description": "clinician_id exactly as returned by check_availability."},
                "start_time": _string("start_time exactly as returned by check_availability."),
            },
            ["service_code", "clinician_id", "start_time"],
        ),
        webhook(
            "get_my_appointments",
            "List the verified patient's upcoming appointments.",
            {},
            [],
        ),
        webhook(
            "reschedule_appointment",
            "Move a verified patient's existing appointment to a new slot from check_availability, after they "
            "confirm. The booking reference stays the same.",
            {
                "appointment_reference": _string("The booking reference of the appointment to move. Empty if they have only one."),
                "new_start_time": _string("start_time of the new slot, exactly as returned by check_availability."),
                "clinician_id": {"type": "integer", "description": "clinician_id of the new slot, as returned by check_availability."},
            },
            ["new_start_time"],
        ),
        webhook(
            "cancel_appointment",
            "Cancel a verified patient's appointment after they confirm they want to cancel.",
            {
                "appointment_reference": _string("The booking reference to cancel. Empty if they have only one."),
                "reason": _string("Brief reason for cancelling, if offered. Empty otherwise."),
            },
            [],
        ),
        webhook(
            "request_callback",
            "Ask the clinic team to phone the patient back: for prescriptions, results, complaints, clinical "
            "questions, or anything you can't handle.",
            {
                "reason": _string("Short, non-clinical summary of why they want a callback."),
                "phone": _string("Number to call back. Leave empty if the patient is verified (we have their number)."),
                "name": _string("Caller's name if not verified. Empty otherwise."),
                "preferred_time": _string("When they'd prefer a call, if they said. Empty otherwise."),
            },
            ["reason"],
        ),
        webhook(
            "start_emergency_call",
            "EMERGENCY: immediately connect the patient to the clinic's doctors on a live call - every available "
            "doctor is phoned at once and joins the patient; you leave the call. Use it straight away, without "
            "further questions, when the patient says it's an emergency, urgently asks for a doctor, or describes "
            "emergency symptoms.",
            {"reason": _string("A few words on what's happening, e.g. 'chest pain, short of breath'. Empty if unknown.")},
            [],
            timeout=10,
        ),
        webhook(
            "connect_to_reception",
            "Put the patient through to a person at reception - when they ask to speak to someone, or you can't help.",
            {"reason": _string("Briefly, why they want to speak to someone. Empty if not given.")},
            [],
        ),
    ]


def _system_tool(name: str) -> dict[str, Any]:
    return {"type": "system", "name": name, "description": "", "params": {"system_tool_type": name}}


# --- ElevenLabs resources ------------------------------------------------------------


def ensure_secret(api: ElevenLabsAdmin, state: dict[str, Any], key: str, value: str) -> tuple[str, str | None]:
    """Returns (secret_id, superseded_secret_id). Secrets are replaced, never edited in place."""
    secrets_state = state.setdefault("secrets", {})
    entry = secrets_state.get(key)
    if entry and entry.get("fingerprint") == fingerprint(value):
        existing = api.call("GET", "/v1/convai/secrets").get("secrets", [])
        if any(s.get("secret_id") == entry["id"] for s in existing):
            return entry["id"], None
    created = api.call(
        "POST", "/v1/convai/secrets", json={"type": "new", "name": f"{key}_{int(time.time())}", "value": value}
    )
    secrets_state[key] = {"id": created["secret_id"], "fingerprint": fingerprint(value)}
    return created["secret_id"], entry["id"] if entry else None


def ensure_tool(api: ElevenLabsAdmin, state: dict[str, Any], config: dict[str, Any]) -> str:
    tools_state = state.setdefault("tools", {})
    tool_id = tools_state.get(config["name"])
    if tool_id and api.call("PATCH", f"/v1/convai/tools/{tool_id}", json={"tool_config": config}, allow_404=True) is not None:
        return tool_id
    created = api.call("POST", "/v1/convai/tools", json={"tool_config": config})
    tools_state[config["name"]] = created["id"]
    return created["id"]


def ensure_documents(api: ElevenLabsAdmin, state: dict[str, Any], clinic_name: str) -> tuple[list[dict[str, Any]], list[str]]:
    docs_state: dict[str, Any] = state.setdefault("documents", {})
    refs: list[dict[str, Any]] = []
    stale: list[str] = []
    present = set()
    for path in sorted(KB_DIR.glob("*.md")):
        present.add(path.stem)
        text = path.read_text(encoding="utf-8")
        entry = docs_state.get(path.stem)
        if entry and entry.get("fingerprint") == fingerprint(text):
            if api.call("GET", f"/v1/convai/knowledge-base/{entry['id']}", allow_404=True) is not None:
                refs.append({"type": "text", "name": entry["name"], "id": entry["id"], "usage_mode": "prompt"})
                continue
        name = f"{clinic_name} - {path.stem.replace('_', ' ')}"
        created = api.call("POST", "/v1/convai/knowledge-base/text", json={"text": text, "name": name})
        if entry:
            stale.append(entry["id"])
        docs_state[path.stem] = {"id": created["id"], "name": created.get("name", name), "fingerprint": fingerprint(text)}
        refs.append({"type": "text", "name": docs_state[path.stem]["name"], "id": created["id"], "usage_mode": "prompt"})
    for stem in list(docs_state):
        if stem not in present:
            stale.append(docs_state.pop(stem)["id"])
    return refs, stale


def _voice_in_account(api: ElevenLabsAdmin, voice_id: str) -> bool:
    try:
        api.call("GET", f"/v1/voices/{voice_id}")
        return True
    except ApiError as exc:
        if exc.status in (400, 404):  # ElevenLabs answers 400 "voice_not_found"
            return False
        raise


def _add_from_voice_library(api: ElevenLabsAdmin, voice_id: str, log: Callable[[str], None]) -> str | None:
    """A Voice Library voice has to be added to the account's voices before an agent can use it."""
    shared = api.call("GET", "/v1/shared-voices", params={"search": voice_id, "page_size": 30}).get("voices", [])
    match = next((v for v in shared if v.get("voice_id") == voice_id), None)
    if match is None:
        return None
    name = match.get("name") or "Receptionist voice"
    if match.get("live_moderation_enabled"):
        log(f"  [!!] '{name}' has live moderation turned on by its owner, so ElevenLabs agents can't use it.")
        return None
    try:
        added = api.call("POST", f"/v1/voices/add/{match['public_owner_id']}/{voice_id}", json={"new_name": name})
    except ApiError as exc:
        log(f"  [!!] Couldn't add '{name}' from the Voice Library (HTTP {exc.status}): {exc.body[:200]}")
        return None
    log(f"Added '{name}' from the Voice Library to your voices.")
    return added.get("voice_id") or voice_id


def resolve_voice(api: ElevenLabsAdmin, settings: Settings, state: dict[str, Any], log: Callable[[str], None]) -> str:
    wanted = settings.elevenlabs_voice_id
    if wanted:
        if _voice_in_account(api, wanted):
            return wanted
        added = _add_from_voice_library(api, wanted, log)
        if added:
            return added
        # Don't let a typo block the rest of the update (tool and LLM URLs must stay current).
        log(
            f"  [!!] ELEVENLABS_VOICE_ID {wanted} isn't in your ElevenLabs voices or the Voice Library - "
            "keeping the current voice. Copy the ID from ElevenLabs > Voices."
        )
    previous = state.get("voice_id")
    if previous and previous != wanted and _voice_in_account(api, previous):
        return previous
    voices = api.call("GET", "/v1/voices").get("voices", [])

    def score(voice: dict[str, Any]) -> tuple[int, int]:
        labels = {k: str(v).lower() for k, v in (voice.get("labels") or {}).items()}
        return (
            0 if labels.get("age", "").startswith("young") else 1,
            0 if "conversational" in labels.get("use_case", "") or "conversational" in labels.get("use case", "") else 1,
        )

    british_female = [
        v
        for v in voices
        if "british" in str((v.get("labels") or {}).get("accent", "")).lower()
        and str((v.get("labels") or {}).get("gender", "")).lower() == "female"
    ]
    if not british_female:
        raise SetupError(
            "No British female voice found in your ElevenLabs voice library.\n"
            "Run `python -m app.cli voices search` to find one, then `python -m app.cli voices add ...`."
        )
    chosen = sorted(british_female, key=score)[0]
    labels = chosen.get("labels") or {}
    log(f"Using voice '{chosen.get('name')}' ({chosen['voice_id']}; {labels.get('accent')}, {labels.get('age')}).")
    return chosen["voice_id"]


def agent_payload(
    settings: Settings,
    clinic: ClinicSnapshot,
    *,
    prompt: str,
    tool_ids: list[str],
    documents: list[dict[str, Any]],
    llm_secret_id: str,
    voice_id: str,
    token_var: str,
    tool_secret_id: str | None = None,
    plain_webhook_header: bool = False,
) -> dict[str, Any]:
    from ..services.reminders import voicemail_message

    telephony = settings.telephony_configured
    surnames = sorted({c.full_name.split()[-1] for c in clinic.clinicians})
    keywords = [*settings.clinic_name.split()[:1], *surnames, "physiotherapy", "phlebotomy", "GP", "mobile"]
    return {
        "name": f"{settings.clinic_name} - AI receptionist",
        "tags": ["healthcare", "uk", "receptionist", "web", *(["phone"] if telephony else [])],
        "conversation_config": {
            "agent": {
                "first_message": (
                    f"Hello, and welcome to {settings.clinic_name}. I'm {settings.assistant_name}, the clinic's "
                    "AI assistant. How can I help you today?"
                ),
                "language": "en",
                "dynamic_variables": {
                    "dynamic_variable_placeholders": {
                        token_var: "dashboard-preview",
                        "channel": "web",
                        "caller_known": "no",
                        "call_purpose": "general",
                        "appointment_summary": "none",
                    }
                },
                "prompt": {
                    "prompt": prompt,
                    "llm": "custom-llm",
                    "custom_llm": {
                        "url": f"{settings.public_base_url}/v1",
                        "model_id": settings.vertex_model,
                        "api_key": {"secret_id": llm_secret_id},
                    },
                    "temperature": 0.3,
                    "tool_ids": tool_ids,
                    "built_in_tools": {
                        "end_call": _system_tool("end_call"),
                        "skip_turn": _system_tool("skip_turn"),
                        "voicemail_detection": {
                            **_system_tool("voicemail_detection"),
                            "params": {"system_tool_type": "voicemail_detection", "voicemail_message": voicemail_message()},
                        },
                    },
                    "knowledge_base": documents,
                    "timezone": settings.clinic_timezone,
                },
            },
            "tts": {
                "model_id": settings.elevenlabs_tts_model,
                "voice_id": voice_id,
                "stability": 0.55,
                "similarity_boost": 0.8,
                "speed": 1.0,
            },
            "asr": {"keywords": keywords},
            "turn": {"turn_timeout": 8, "turn_eagerness": "normal"},
            "conversation": {"max_duration_seconds": 900},
        },
        "platform_settings": {
            "auth": {"enable_auth": True},
            "overrides": {
                # text_only: the web chat mode; first_message: personalised openings for reminder calls.
                "conversation_config_override": {"conversation": {"text_only": True}, "agent": {"first_message": True}},
                **({"enable_conversation_initiation_client_data_from_webhook": True} if telephony else {}),
            },
            **(
                {
                    "workspace_overrides": {
                        "conversation_initiation_client_data_webhook": {
                            "url": f"{settings.public_base_url}/webhooks/elevenlabs/call-start",
                            "request_headers": {
                                "X-Webhook-Secret": settings.tool_secret.get_secret_value()
                                if plain_webhook_header or not tool_secret_id
                                else {"secret_id": tool_secret_id}
                            },
                        }
                    }
                }
                if telephony
                else {}
            ),
            "evaluation": {
                "criteria": [
                    {
                        "id": "request_resolved",
                        "name": "Request resolved",
                        "conversation_goal_prompt": "The patient's request was completed (appointment booked, "
                        "rescheduled or cancelled, or question answered) or a callback was arranged.",
                    },
                    {
                        "id": "no_medical_advice",
                        "name": "No medical advice",
                        "conversation_goal_prompt": "The assistant did not diagnose, interpret results or advise "
                        "on medication or treatment.",
                    },
                    {
                        "id": "identity_before_disclosure",
                        "name": "Identity checked before disclosure",
                        "conversation_goal_prompt": "The assistant did not reveal or change existing appointment "
                        "details before the patient's identity was verified.",
                    },
                    {
                        "id": "emergency_handled",
                        "name": "Emergency signposting",
                        "conversation_goal_prompt": "If the patient described emergency symptoms, the assistant "
                        "told them to call 999 or go to A&E. Mark success if no emergency was mentioned.",
                    },
                ]
            },
            "data_collection": {
                "intent": {
                    "type": "string",
                    "description": "Main reason for contact: book, reschedule, cancel, enquiry, callback, emergency or other.",
                },
                "outcome": {
                    "type": "string",
                    "description": "Result: booked, rescheduled, cancelled, answered, callback_requested, "
                    "emergency_signposted, abandoned or failed.",
                },
                "appointment_reference": {
                    "type": "string",
                    "description": "Booking reference created or changed in this conversation, if any.",
                },
                "service_requested": {"type": "string", "description": "The service_code discussed, if any."},
                "new_patient": {"type": "boolean", "description": "True if a new patient was registered."},
            },
            "call_limits": {"daily_limit": settings.elevenlabs_daily_conversation_limit},
            "privacy": {"record_voice": False, "retention_days": 30},
        },
    }


def _save_agent(api: ElevenLabsAdmin, payload: dict[str, Any], agent_id: str | None, log: Callable[[str], None]) -> str:
    if agent_id and api.call("PATCH", f"/v1/convai/agents/{agent_id}", json=payload, allow_404=True) is not None:
        log(f"Updated agent {agent_id}.")
        return agent_id
    new_id: str = api.call("POST", "/v1/convai/agents/create", json=payload)["agent_id"]
    log(f"Created agent {new_id}.")
    return new_id


def setup_phone_line(
    api: ElevenLabsAdmin, settings: Settings, state: dict[str, Any], agent_id: str, log: Callable[[str], None]
) -> None:
    """Browser-calling TwiML app (Twilio) + the clinic's number connected to the agent (ElevenLabs)."""
    from ..services.telephony import TwilioTelephony, to_e164

    twilio = TwilioTelephony(settings, state.get("twiml_app_sid"))
    if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
        try:
            voice_url = f"{settings.public_base_url}/twilio/voice/client"
            state["twiml_app_sid"] = twilio.ensure_twiml_app(voice_url, state.get("twiml_app_sid"))
            log(f"  Browser calling ready (TwiML app {state['twiml_app_sid']}).")
        except Exception as exc:  # Twilio errors are reported, not fatal
            log(f"  (could not set up browser calling: {exc})")
    else:
        log("  Browser calling (web SOS / reception) needs TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET - skipped.")

    if settings.twilio_auth_token is None:
        log("  Set TWILIO_AUTH_TOKEN to connect the number to the agent - skipped.")
        return
    number = to_e164(settings.twilio_phone_number or "")
    listing = api.call("GET", "/v1/convai/phone-numbers")
    numbers = listing if isinstance(listing, list) else listing.get("phone_numbers", [])
    match = next((n for n in numbers if n.get("phone_number") == number), None)
    if match:
        phone_number_id = match["phone_number_id"]
        assigned = (match.get("assigned_agent") or {}).get("agent_id")
        if assigned != agent_id:
            try:
                api.call("PATCH", f"/v1/convai/phone-numbers/{phone_number_id}", json={"agent_id": agent_id})
                log(f"  {number} is now answered by the agent.")
            except ApiError as exc:
                log(f"  (assign {number} to the agent in the ElevenLabs dashboard - HTTP {exc.status})")
        else:
            log(f"  {number} is answered by the agent.")
    else:
        auth_token = settings.twilio_auth_token.get_secret_value()
        body: dict[str, Any] = {
            "provider": "twilio",
            "phone_number": number,
            "label": f"{settings.clinic_name} line",
            "sid": settings.twilio_account_sid,
            "token": auth_token,
            "agent_id": agent_id,
        }
        if settings.twilio_region:
            regional = settings.twilio_api_key_secret.get_secret_value() if settings.twilio_api_key_secret else auth_token
            body["region_config"] = {"region_id": settings.twilio_region, "token": regional}
            if settings.twilio_edge:
                body["region_config"]["edge_location"] = settings.twilio_edge
        phone_number_id = api.call("POST", "/v1/convai/phone-numbers", json=body)["phone_number_id"]
        log(f"  Connected {number} to the agent.")
    state["phone_number_id"] = phone_number_id


def run_setup(*, force_new_agent: bool = False, log: Callable[[str], None] = print) -> dict[str, Any]:
    settings = get_settings()
    if settings.elevenlabs_api_key is None:
        raise SetupError("Set ELEVENLABS_API_KEY in backend/.env first.")
    if not settings.public_base_url.startswith("https://"):
        raise SetupError(
            "PUBLIC_BASE_URL must be a public https:// URL that ElevenLabs can reach "
            "(e.g. an ngrok or Cloudflare tunnel to http://localhost:8000)."
        )
    if not settings.vertex_project_id:
        raise SetupError("Set VERTEX_PROJECT_ID in backend/.env (the agent uses Vertex AI as its custom LLM).")

    clinic = asyncio.run(load_clinic())
    if not clinic.services or not clinic.clinicians:
        raise SetupError("No services/clinicians in the database. Run `python -m app.cli seed` first.")

    state = load_state()
    api = ElevenLabsAdmin(settings.elevenlabs_api_key.get_secret_value(), settings.elevenlabs_api_base)
    superseded_secrets: list[str] = []
    try:
        log("Secrets...")
        tool_secret_id, old = ensure_secret(api, state, "hc_tool_secret", settings.tool_secret.get_secret_value())
        superseded_secrets += [old] if old else []
        llm_secret_id, old = ensure_secret(api, state, "hc_llm_proxy_secret", settings.llm_proxy_secret.get_secret_value())
        superseded_secrets += [old] if old else []
        save_state(state)

        log("Tools...")
        transports = [state["session_token_transport"]] if state.get("session_token_transport") else list(TOKEN_TRANSPORTS)
        tool_ids: list[str] = []
        last_error: ApiError | None = None
        for transport in transports:
            try:
                tool_ids = [ensure_tool(api, state, cfg) for cfg in tool_configs(settings, clinic, tool_secret_id, transport)]
            except ApiError as exc:
                about_headers = "header" in exc.body.lower() or "variable" in exc.body.lower()
                if exc.status != 422 or not about_headers or len(transports) == 1:
                    raise
                log(f"  ElevenLabs rejected session-token transport '{transport}', trying the next option...")
                last_error = exc
                continue
            state["session_token_transport"] = transport
            state["session_token_var"] = DEFAULT_SESSION_TOKEN_VAR if transport != "body_variable" else "session_token"
            break
        if not tool_ids:
            raise last_error or SetupError("Could not create tools")
        save_state(state)
        log(f"  {len(tool_ids)} tools ready (session token via {state['session_token_transport']}).")

        log("Knowledge base...")
        documents, stale_documents = ensure_documents(api, state, settings.clinic_name)
        save_state(state)

        voice_id = resolve_voice(api, settings, state, log)
        prompt = build_prompt(settings, clinic)

        def payload_for(voice: str, plain_webhook_header: bool) -> dict[str, Any]:
            return agent_payload(
                settings,
                clinic,
                prompt=prompt,
                tool_ids=tool_ids,
                documents=documents,
                llm_secret_id=llm_secret_id,
                voice_id=voice,
                token_var=state["session_token_var"],
                tool_secret_id=tool_secret_id,
                plain_webhook_header=plain_webhook_header,
            )

        agent_id = None if force_new_agent else (settings.elevenlabs_agent_id or state.get("agent_id"))
        plain_webhook_header = False
        for _attempt in range(3):
            try:
                agent_id = _save_agent(api, payload_for(voice_id, plain_webhook_header), agent_id, log)
                break
            except ApiError as exc:
                if settings.telephony_configured and exc.status == 422 and "request_headers" in exc.body and not plain_webhook_header:
                    log("  ElevenLabs wants a plain header value for the call-start webhook - retrying.")
                    plain_webhook_header = True
                    continue
                previous = state.get("voice_id")  # the voice the agent last saved with
                if "tts.voice_id" in exc.body and previous and previous != voice_id:
                    # e.g. a Voice Library voice with live moderation: agents can't use it.
                    log(f"  [!!] ElevenLabs won't use voice {voice_id} for agents: {exc.message}")
                    log("       Keeping the current voice - choose another one for ELEVENLABS_VOICE_ID.")
                    voice_id = previous
                    continue
                raise
        state["voice_id"] = voice_id
        state["agent_id"] = agent_id
        save_state(state)

        if settings.telephony_configured:
            log("Phone line...")
            setup_phone_line(api, settings, state, agent_id, log)
            save_state(state)

        # Clean up anything this run replaced (best effort).
        for doc_id in stale_documents:
            try:
                api.call("DELETE", f"/v1/convai/knowledge-base/{doc_id}", allow_404=True)
            except ApiError as exc:
                log(f"  (could not delete old document {doc_id}: HTTP {exc.status})")
        for secret_id in superseded_secrets:
            try:
                api.call("DELETE", f"/v1/convai/secrets/{secret_id}", allow_404=True)
            except ApiError as exc:
                log(f"  (could not delete old secret {secret_id}: HTTP {exc.status})")
        return state
    finally:
        api.close()
