"""Command-line tasks:  python -m app.cli <command>

  migrate          apply database migrations
  seed             load the sample clinic (services, clinicians, hours, test patients)
  setup-agent      create/update the ElevenLabs agent (tools, knowledge base, prompt, voice, LLM)
  voices           find and add a British female voice from the ElevenLabs voice library
  setup-calendars  create (or find) a Google calendar per clinician and link them
  link-calendar    connect a clinician to a specific Google calendar by id
  check-calendar   verify Google Calendar access for every linked clinician
  check-vertex     verify Vertex AI (project, region, model, tool calling) and measure latency
  doctor           show what is configured and what is missing
  rotate-secrets   replace placeholder secrets with strong random ones (and update the agent)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import timedelta
from typing import Any

import httpx

from .config import BACKEND_DIR, get_settings


def _ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def _warn(msg: str) -> None:
    print(f"  [!!] {msg}")


# --- migrate / seed -----------------------------------------------------------------


def cmd_migrate(_args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    print("Database is up to date.")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from .db import dispose_engine, get_sessionmaker
    from .seed import seed

    async def run() -> dict[str, int]:
        try:
            async with get_sessionmaker()() as db:
                return await seed(db, demo_patients=not args.no_demo_patients)
        finally:
            await dispose_engine()

    result = asyncio.run(run())
    print(f"Seeded {result['services']} services and {result['clinicians']} clinicians; {result['patients_created']} test patients added.")
    if not args.no_demo_patients:
        print("Test patients (synthetic): Alice Testington 12/04/1990 07700 900123 | Ben Sample 03/11/1985 07700 900456 | Chloe Example 01/06/2016 07700 900789")
    return 0


# --- ElevenLabs ------------------------------------------------------------------------


def cmd_setup_agent(args: argparse.Namespace) -> int:
    from .agent.setup import SetupError, run_setup

    try:
        state = run_setup(force_new_agent=args.new)
    except SetupError as exc:
        print(f"\nSetup failed:\n{exc}", file=sys.stderr)
        return 1
    settings = get_settings()
    print("\nAgent ready.")
    print(f"  Agent id:      {state['agent_id']}")
    print(f"  Dashboard:     https://elevenlabs.io/app/agents/agents/{state['agent_id']}")
    print(f"  Tools call:    {settings.public_base_url}/tools/*")
    print(f"  LLM proxy:     {settings.public_base_url}/v1/chat/completions  ->  Vertex AI {settings.vertex_model} ({settings.vertex_location})")
    print("\nOptional: in ElevenLabs > Agents > Settings > Webhooks, add a post-call webhook to")
    print(f"  {settings.public_base_url}/webhooks/elevenlabs  and put its secret in ELEVENLABS_WEBHOOK_SECRET.")
    return 0


def _elevenlabs_client() -> httpx.Client:
    settings = get_settings()
    if settings.elevenlabs_api_key is None:
        raise SystemExit("Set ELEVENLABS_API_KEY in backend/.env first.")
    return httpx.Client(
        base_url=settings.elevenlabs_api_base,
        headers={"xi-api-key": settings.elevenlabs_api_key.get_secret_value()},
        timeout=30,
    )


def cmd_voices(args: argparse.Namespace) -> int:
    from .services.elevenlabs import load_state, save_state

    with _elevenlabs_client() as client:
        if args.action == "search":
            params: dict[str, Any] = {
                "page_size": args.limit,
                "gender": "female",
                "accent": "british",
                "language": "en",
            }
            if args.age:
                params["age"] = args.age
            if args.use_case:
                params["use_cases"] = args.use_case
            response = client.get("/v1/shared-voices", params=params)
            response.raise_for_status()
            voices = response.json().get("voices", [])
            if not voices:
                print("No voices matched. Try --age '' or --use-case ''.")
            for v in voices:
                print(f"\n{v.get('name')}  [{v.get('accent')}, {v.get('age')}, {v.get('use_case')}]")
                print(f"  {str(v.get('description') or '').strip()[:160]}")
                print(f"  preview: {v.get('preview_url')}")
                print(f"  add with: python -m app.cli voices add {v.get('public_owner_id')} {v.get('voice_id')} \"{v.get('name')}\"")
            return 0
        if args.action == "add":
            response = client.post(
                f"/v1/voices/add/{args.public_owner_id}/{args.voice_id}", json={"new_name": args.name or "Receptionist voice"}
            )
            if response.status_code >= 400:
                print(f"Could not add voice: {response.status_code} {response.text}", file=sys.stderr)
                return 1
            voice_id = response.json()["voice_id"]
            state = load_state()
            state["voice_id"] = voice_id
            save_state(state)
            print(f"Added to your library as {voice_id} and selected for the agent. Run `python -m app.cli setup-agent` to apply.")
            return 0
        if args.action == "list":
            response = client.get("/v1/voices")
            response.raise_for_status()
            for v in response.json().get("voices", []):
                labels = v.get("labels") or {}
                print(f"{v['voice_id']}  {v.get('name'):<28} {labels.get('gender', '')} {labels.get('accent', '')} {labels.get('age', '')}")
            return 0
    return 1


# --- Google Calendar ---------------------------------------------------------------------


def cmd_link_calendar(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from .db import dispose_engine, get_sessionmaker
    from .models import Clinician

    async def run() -> int:
        try:
            async with get_sessionmaker()() as db:
                if args.clinician_id is not None:
                    clinician = await db.get(Clinician, args.clinician_id)
                    if clinician is None:
                        print(f"No clinician with id {args.clinician_id}", file=sys.stderr)
                        return 1
                    clinician.google_calendar_id = args.calendar_id or None
                    await db.commit()
                    print(f"{clinician.full_name}: {'linked to ' + args.calendar_id if args.calendar_id else 'unlinked'}")
                for c in (await db.execute(select(Clinician).order_by(Clinician.id))).scalars():
                    print(f"  [{c.id}] {c.full_name:<22} {c.google_calendar_id or '(no calendar)'}")
                return 0
        finally:
            await dispose_engine()

    return asyncio.run(run())


def _google_calendar() -> Any:
    """GoogleCalendar from settings (service-account key, or your gcloud login), or None."""
    from .services.calendar import GoogleCalendar

    settings = get_settings()
    try:
        gateway = GoogleCalendar.from_settings(settings)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return None
    print(f"Google identity: {gateway.identity}")
    if settings.google_impersonate_user:
        print(f"Impersonating:   {settings.google_impersonate_user} (domain-wide delegation)")
    return gateway


def _calendar_hint(exc: Exception, gateway: Any) -> None:
    from .services.calendar import explain_calendar_error

    hint = explain_calendar_error(str(exc), gateway.quota_project or get_settings().vertex_project_id)
    if hint:
        print(f"       -> {hint}")


def cmd_setup_calendars(args: argparse.Namespace) -> int:
    """Create (or find) one Google calendar per clinician and link them."""
    from sqlalchemy import select

    from .db import dispose_engine, get_sessionmaker
    from .models import Clinician
    from .services.calendar import CalendarError, plan_calendar_links

    settings = get_settings()
    gateway = _google_calendar()
    if gateway is None:
        return 1
    prefix = f"{settings.clinic_name} - "

    async def run() -> int:
        try:
            calendars = await gateway.list_calendars()
        except CalendarError as exc:
            _warn(str(exc))
            _calendar_hint(exc, gateway)
            return 1
        try:
            async with get_sessionmaker()() as db:
                clinicians = list(
                    (await db.execute(select(Clinician).where(Clinician.is_active.is_(True)).order_by(Clinician.id))).scalars()
                )
                plan = plan_calendar_links([(c.id, c.full_name, c.google_calendar_id) for c in clinicians], calendars, prefix)
                by_id = {c.id: c for c in clinicians}
                for step in plan:
                    if step.action == "create":
                        if args.dry_run:
                            print(f"  [{step.clinician_id}] {step.clinician_name:<22} would create '{step.calendar_name}'")
                            continue
                        try:
                            step.calendar_id = await gateway.create_calendar(
                                step.calendar_name, f"Appointments booked by the {settings.clinic_name} AI receptionist."
                            )
                            if args.share_with:
                                await gateway.share_calendar(step.calendar_id, args.share_with)
                        except CalendarError as exc:
                            _warn(f"{step.clinician_name}: {exc}")
                            _calendar_hint(exc, gateway)
                            return 1
                    verb = {"keep": "already linked to", "link": "linked to existing", "create": "created and linked"}[step.action]
                    print(f"  [{step.clinician_id}] {step.clinician_name:<22} {verb} '{step.calendar_name}'")
                    if not args.dry_run and step.calendar_id:
                        by_id[step.clinician_id].google_calendar_id = step.calendar_id
                if not args.dry_run:
                    await db.commit()
        finally:
            await dispose_engine()
        if args.dry_run:
            print("\nDry run: nothing was changed.")
        else:
            print("\nNext: set GOOGLE_CALENDAR_ENABLED=true in backend/.env, restart the backend, and run")
            print("      python -m app.cli check-calendar")
        return 0

    return asyncio.run(run())


def cmd_check_calendar(_args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from . import timeutil
    from .db import dispose_engine, get_sessionmaker
    from .models import Clinician
    from .services.calendar import CalendarError, CalendarEvent

    gateway = _google_calendar()
    if gateway is None:
        return 1

    async def run() -> int:
        failures = 0
        try:
            async with get_sessionmaker()() as db:
                clinicians = [c for c in (await db.execute(select(Clinician).order_by(Clinician.id))).scalars() if c.google_calendar_id]
        finally:
            await dispose_engine()
        if not clinicians:
            _warn("No clinician has a calendar yet. Run: python -m app.cli setup-calendars")
            return 1
        now = timeutil.now_utc()
        for c in clinicians:
            print(f"\n{c.full_name} -> {c.google_calendar_id}")
            try:
                busy = await gateway.busy_intervals([c.google_calendar_id], now, now + timedelta(days=7))
                _ok(f"free/busy readable ({len(busy.get(c.google_calendar_id, []))} busy blocks in the next 7 days)")
                event_id = f"hctest{uuid.uuid4().hex}"
                start = now + timedelta(days=365)
                await gateway.upsert_event(
                    c.google_calendar_id,
                    event_id,
                    CalendarEvent("AI receptionist connection test", "Safe to ignore.", start, start + timedelta(minutes=5)),
                )
                await gateway.delete_event(c.google_calendar_id, event_id)
                _ok("can create and delete events")
            except CalendarError as exc:
                failures += 1
                _warn(str(exc))
                _calendar_hint(exc, gateway)
        if not failures and not get_settings().google_calendar_enabled:
            print("\nAll good. Set GOOGLE_CALENDAR_ENABLED=true in backend/.env and restart the backend to switch it on.")
        return 1 if failures else 0

    return asyncio.run(run())


# --- Vertex AI -----------------------------------------------------------------------------


def cmd_check_vertex(_args: argparse.Namespace) -> int:
    from .llm.vertex_proxy import VertexTokenProvider

    settings = get_settings()
    if not settings.vertex_project_id:
        print("Set VERTEX_PROJECT_ID in backend/.env", file=sys.stderr)
        return 1
    print(f"Project {settings.vertex_project_id} | location {settings.vertex_location} | model {settings.vertex_model}")

    async def run() -> int:
        tokens = VertexTokenProvider(settings.vertex_credentials_file)
        try:
            auth_headers = await tokens.auth_headers()
        except Exception as exc:
            _warn(f"Could not get Google credentials: {exc}")
            print("Set VERTEX_CREDENTIALS_FILE to a service-account JSON key with the 'Vertex AI User' role,")
            print("or run `gcloud auth application-default login`.")
            return 1
        _ok(f"Google credentials OK: {tokens.describe()}")
        url = f"{settings.vertex_base_url}/chat/completions"
        tool = {
            "type": "function",
            "function": {
                "name": "check_availability",
                "description": "Find appointment slots",
                "parameters": {"type": "object", "properties": {"service_code": {"type": "string"}}, "required": ["service_code"]},
            },
        }
        tests = [
            ("plain reply", {"messages": [{"role": "user", "content": "Say hello in five words, British English."}]}),
            ("tool call", {"messages": [{"role": "user", "content": "Check GP availability please."}], "tools": [tool]}),
        ]
        async with httpx.AsyncClient(timeout=60) as client:
            for label, body in tests:
                payload = {"model": settings.vertex_model, "stream": True, **body}
                if settings.vertex_reasoning_effort:
                    payload["reasoning_effort"] = settings.vertex_reasoning_effort
                started = time.perf_counter()
                first = None
                text_out, tool_calls = "", 0
                async with client.stream("POST", url, json=payload, headers=auth_headers) as r:
                    if r.status_code != 200:
                        _warn(f"{label}: HTTP {r.status_code}: {(await r.aread()).decode()[:500]}")
                        if r.status_code == 404:
                            print("   The model may not be available in this region; try VERTEX_LOCATION=global or another VERTEX_MODEL.")
                        return 1
                    async for line in r.aiter_lines():
                        if not line.startswith("data:") or line.strip() == "data: [DONE]":
                            continue
                        first = first or time.perf_counter()
                        chunk = json.loads(line[5:])
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            text_out += delta.get("content") or ""
                            tool_calls += len(delta.get("tool_calls") or [])
                ttft = ((first or time.perf_counter()) - started) * 1000
                detail = f"tool calls: {tool_calls}" if label == "tool call" else repr(text_out.strip()[:60])
                _ok(f"{label}: first token {ttft:.0f} ms, total {(time.perf_counter() - started) * 1000:.0f} ms, {detail}")
        return 0

    return asyncio.run(run())


# --- doctor ------------------------------------------------------------------------------


def cmd_doctor(_args: argparse.Namespace) -> int:
    from .services.elevenlabs import load_state, session_token_var

    settings = get_settings()
    state = load_state()
    checks = [
        ("Database URL", settings.database_url.split("@")[-1]),
        ("Public base URL", settings.public_base_url + ("" if settings.public_base_url.startswith("https://") else "  <- must be public https for ElevenLabs")),
        ("ElevenLabs API key", "set" if settings.elevenlabs_api_key else "MISSING"),
        ("ElevenLabs agent", settings.elevenlabs_agent_id or state.get("agent_id") or "not created yet (run setup-agent)"),
        ("Session token variable", session_token_var()),
        ("Post-call webhook secret", "set" if settings.elevenlabs_webhook_secret else "not set (optional)"),
        ("Vertex project", settings.vertex_project_id or "MISSING"),
        ("Vertex location/model", f"{settings.vertex_location} / {settings.vertex_model}"),
        ("Vertex credentials", str(settings.vertex_credentials_file or "application default credentials")),
        (
            "Google Calendar",
            ("enabled" if settings.google_calendar_enabled else "disabled")
            + (f" (key file {settings.google_service_account_file})" if settings.google_service_account_file else " (uses your gcloud login)"),
        ),
        ("Max concurrent sessions", str(settings.max_concurrent_sessions)),
        (
            "Twilio phone line",
            f"{settings.twilio_phone_number} ("
            + ("answered by the agent" if state.get("phone_number_id") else "run setup-agent to connect it")
            + ")"
            if settings.telephony_configured
            else "not set up (phone calls, SMS and the SOS line are off)",
        ),
        (
            "Browser calling (SOS)",
            "ready"
            if settings.twilio_api_key_sid and (settings.twilio_twiml_app_sid or state.get("twiml_app_sid"))
            else "needs TWILIO_API_KEY_SID/SECRET, then setup-agent",
        ),
        ("Reception number", settings.reception_phone_number or "not set (Sophie offers a callback instead)"),
        ("SMS confirmations", "on" if settings.telephony_configured and settings.sms_enabled else "off"),
        ("Reminder calls", f"on, from {settings.reminder_call_hour}:00" if settings.reminder_calls_enabled else "off"),
    ]
    for name, value in checks:
        print(f"  {name:<26} {value}")
    for name in settings.placeholder_secrets():
        _warn(f"{name.upper()} is still a placeholder - run: python -m app.cli rotate-secrets")
    if settings.telephony_configured and settings.twilio_auth_token is None and settings.twilio_validate_signatures:
        _warn("TWILIO_AUTH_TOKEN is not set: Twilio's webhooks (SOS, transfers) can't be verified and will be refused")
    return 0


def cmd_rotate_secrets(args: argparse.Namespace) -> int:
    """Generate strong TOOL_SECRET / LLM_PROXY_SECRET / ADMIN_TOKEN values and re-point the agent."""
    import secrets as pysecrets

    from .envfile import update_env_file

    env_path = BACKEND_DIR / ".env"
    if not env_path.exists():
        print("backend/.env not found - copy .env.example to .env first.", file=sys.stderr)
        return 1
    update_env_file(env_path, {name: pysecrets.token_urlsafe(32) for name in ("TOOL_SECRET", "LLM_PROXY_SECRET", "ADMIN_TOKEN")})
    print("New random TOOL_SECRET, LLM_PROXY_SECRET and ADMIN_TOKEN written to backend/.env (values not shown).")
    print("Sign in to the staff dashboard with the new ADMIN_TOKEN from backend/.env.")
    get_settings.cache_clear()
    if not args.no_agent:
        print("\nUpdating the ElevenLabs agent to use the new secrets...")
        if cmd_setup_agent(argparse.Namespace(new=False)) != 0:
            print("\nThe agent wasn't updated - run `python -m app.cli setup-agent` once it's fixed.", file=sys.stderr)
            return 1
    print("\nNow restart the backend (Ctrl+C in its window, then dev.cmd) so it uses the new secrets.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    p = sub.add_parser("seed")
    p.add_argument("--no-demo-patients", action="store_true")
    p.set_defaults(func=cmd_seed)
    p = sub.add_parser("setup-agent")
    p.add_argument("--new", action="store_true", help="create a new agent instead of updating the existing one")
    p.set_defaults(func=cmd_setup_agent)

    p = sub.add_parser("voices")
    voice_sub = p.add_subparsers(dest="action", required=True)
    s = voice_sub.add_parser("search")
    s.add_argument("--age", default="young")
    s.add_argument("--use-case", default="conversational")
    s.add_argument("--limit", type=int, default=12)
    a = voice_sub.add_parser("add")
    a.add_argument("public_owner_id")
    a.add_argument("voice_id")
    a.add_argument("name", nargs="?")
    voice_sub.add_parser("list")
    p.set_defaults(func=cmd_voices)

    p = sub.add_parser("link-calendar")
    p.add_argument("clinician_id", type=int, nargs="?")
    p.add_argument("calendar_id", nargs="?", default="")
    p.set_defaults(func=cmd_link_calendar)
    p = sub.add_parser("setup-calendars")
    p.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    p.add_argument("--share-with", metavar="EMAIL", help="also share newly created calendars with this person (useful with a service account)")
    p.set_defaults(func=cmd_setup_calendars)
    sub.add_parser("check-calendar").set_defaults(func=cmd_check_calendar)
    sub.add_parser("check-vertex").set_defaults(func=cmd_check_vertex)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    p = sub.add_parser("rotate-secrets")
    p.add_argument("--no-agent", action="store_true", help="only update backend/.env; don't re-run setup-agent")
    p.set_defaults(func=cmd_rotate_secrets)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
