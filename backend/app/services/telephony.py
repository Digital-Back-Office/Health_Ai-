"""Twilio: SMS, browser calling, call hand-offs and the SOS conference line.

ElevenLabs answers the clinic's Twilio number itself (native integration). The
backend only takes over a call when a *person* is needed:

* SOS: the patient's call (or browser) is moved into a private conference and
  every available doctor is phoned; whoever presses 1 joins. The AI is not on it.
* "Talk to a person": the call is redirected to the reception number.
* SMS confirmations, and access tokens so web patients can join calls from the browser.

Everything is behind the Telephony protocol so the app (and tests) work without
Twilio configured.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Protocol
from xml.sax.saxutils import escape

from ..config import Settings

log = logging.getLogger(__name__)

# British English neural voice for the few things Twilio itself says.
SAY_VOICE = "Polly.Amy-Neural"
SAY_LANGUAGE = "en-GB"


class TelephonyError(Exception):
    """A Twilio operation failed, or telephony isn't configured."""


def to_e164(number: str, default_country_code: str = "44") -> str:
    """'07700 900123' -> '+447700900123'; keeps numbers already in +E.164 form."""
    digits = re.sub(r"[^\d+]", "", number or "")
    if digits.startswith("+"):
        result = "+" + digits[1:].replace("+", "")
    elif digits.startswith("00"):
        result = "+" + digits[2:]
    elif digits.startswith("0"):
        result = f"+{default_country_code}{digits[1:]}"
    else:
        result = f"+{digits}"
    if not re.fullmatch(r"\+\d{8,15}", result):
        raise TelephonyError(f"'{number}' is not a valid phone number")
    return result


def from_e164_uk(number: str) -> str:
    """'+447700900123' -> '07700900123' (how patient phones are stored); other countries unchanged."""
    digits = re.sub(r"[^\d+]", "", number or "")
    if digits.startswith("+44"):
        return "0" + digits[3:]
    return digits


# --- TwiML -------------------------------------------------------------------------------
# Built by hand (it's tiny) so it's easy to read and to test.


def _say(text: str) -> str:
    return f'<Say voice="{SAY_VOICE}" language="{SAY_LANGUAGE}">{escape(text)}</Say>'


def _conference(name: str, *, start_on_enter: bool, end_on_exit: bool, status_callback: str, beep: bool) -> str:
    return (
        f'<Conference startConferenceOnEnter="{str(start_on_enter).lower()}" '
        f'endConferenceOnExit="{str(end_on_exit).lower()}" beep="{str(beep).lower()}" '
        f'statusCallback="{escape(status_callback)}" statusCallbackMethod="POST" '
        f'statusCallbackEvent="start end join leave">{escape(name)}</Conference>'
    )


def twiml_patient_conference(conference: str, status_callback: str, intro: str | None = None) -> str:
    """The patient waits (hold music) until the first doctor joins; when they hang up, the call ends for all."""
    parts = [_say(intro)] if intro else []
    parts.append(
        "<Dial>"
        + _conference(conference, start_on_enter=False, end_on_exit=True, status_callback=status_callback, beep=False)
        + "</Dial>"
    )
    parts.append(_say("The call has ended. If you still need urgent help, call 999."))
    return "<Response>" + "".join(parts) + "</Response>"


def twiml_doctor_prompt(accept_url: str, summary: str) -> str:
    """Doctors must press 1, so a voicemail box can never join the patient's call."""
    message = f"Health AI emergency. {summary} Press 1 to join the call now."
    return (
        "<Response>"
        f'<Gather numDigits="1" timeout="10" action="{escape(accept_url)}" method="POST">'
        + _say(message)
        + _say(message)
        + "</Gather>"
        + _say("No key pressed. Goodbye.")
        + "<Hangup/></Response>"
    )


def twiml_doctor_join(conference: str, status_callback: str) -> str:
    return (
        "<Response>"
        + _say("Connecting you to the patient now.")
        + "<Dial>"
        + _conference(conference, start_on_enter=True, end_on_exit=False, status_callback=status_callback, beep=True)
        + "</Dial></Response>"
    )


def twiml_say_and_hangup(message: str) -> str:
    return "<Response>" + _say(message) + "<Hangup/></Response>"


def twiml_dial_number(number: str, caller_id: str, action_url: str, intro: str | None = None, timeout: int = 25) -> str:
    parts = [_say(intro)] if intro else []
    parts.append(
        f'<Dial callerId="{escape(caller_id)}" timeout="{timeout}" action="{escape(action_url)}" method="POST">'
        f"<Number>{escape(number)}</Number></Dial>"
    )
    return "<Response>" + "".join(parts) + "</Response>"


# --- gateway -------------------------------------------------------------------------------


class Telephony(Protocol):
    enabled: bool
    from_number: str | None

    async def send_sms(self, to: str, body: str) -> str: ...

    async def call(self, to: str, *, url: str, status_callback: str, timeout: int) -> str: ...

    async def update_call(self, call_sid: str, twiml: str) -> None: ...

    async def hang_up(self, call_sid: str) -> None: ...

    async def end_conference(self, friendly_name: str) -> None: ...

    def browser_token(self, identity: str, ttl: int = 900) -> str: ...

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool: ...


class DisabledTelephony:
    """Used until Twilio settings are added to backend/.env."""

    enabled = False
    from_number = None

    def _fail(self) -> None:
        raise TelephonyError("Telephony isn't configured (set the TWILIO_* settings in backend/.env)")

    async def send_sms(self, to: str, body: str) -> str:
        self._fail()
        return ""

    async def call(self, to: str, *, url: str, status_callback: str, timeout: int) -> str:
        self._fail()
        return ""

    async def update_call(self, call_sid: str, twiml: str) -> None:
        self._fail()

    async def hang_up(self, call_sid: str) -> None:
        self._fail()

    async def end_conference(self, friendly_name: str) -> None:
        self._fail()

    def browser_token(self, identity: str, ttl: int = 900) -> str:
        self._fail()
        return ""

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        return False


class TwilioTelephony:
    enabled = True

    def __init__(self, settings: Settings, twiml_app_sid: str | None = None) -> None:
        from twilio.rest import Client

        self._settings = settings
        if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
            self._client = Client(
                settings.twilio_api_key_sid,
                settings.twilio_api_key_secret.get_secret_value(),
                settings.twilio_account_sid,
                region=settings.twilio_region,
                edge=settings.twilio_edge,
            )
        else:
            assert settings.twilio_auth_token is not None
            self._client = Client(
                settings.twilio_account_sid,
                settings.twilio_auth_token.get_secret_value(),
                region=settings.twilio_region,
                edge=settings.twilio_edge,
            )
        self.from_number = to_e164(settings.twilio_phone_number or "")
        self._twiml_app_sid = settings.twilio_twiml_app_sid or twiml_app_sid

    @property
    def twiml_app_sid(self) -> str | None:
        # setup-agent may create the TwiML app while the backend is already running.
        if not self._twiml_app_sid:
            from .elevenlabs import load_state

            self._twiml_app_sid = load_state().get("twiml_app_sid")
        return self._twiml_app_sid

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        from twilio.base.exceptions import TwilioException

        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except TwilioException as exc:
            raise TelephonyError(f"Twilio error: {exc}") from exc
        except Exception as exc:  # network errors
            raise TelephonyError(f"Twilio request failed: {exc}") from exc

    async def send_sms(self, to: str, body: str) -> str:
        sender = self._settings.sms_sender or self.from_number
        message = await self._run(self._client.messages.create, to=to_e164(to), from_=sender, body=body)
        return message.sid

    async def call(self, to: str, *, url: str, status_callback: str, timeout: int) -> str:
        call = await self._run(
            self._client.calls.create,
            to=to_e164(to),
            from_=self.from_number,
            url=url,
            method="POST",
            status_callback=status_callback,
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            timeout=timeout,
        )
        return call.sid

    async def update_call(self, call_sid: str, twiml: str) -> None:
        await self._run(self._client.calls(call_sid).update, twiml=twiml)

    async def hang_up(self, call_sid: str) -> None:
        await self._run(self._client.calls(call_sid).update, status="completed")

    async def end_conference(self, friendly_name: str) -> None:
        conferences = await self._run(self._client.conferences.list, friendly_name=friendly_name, status="in-progress", limit=5)
        for conference in conferences:
            await self._run(self._client.conferences(conference.sid).update, status="completed")

    def browser_token(self, identity: str, ttl: int = 900) -> str:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant

        settings = self._settings
        if not (settings.twilio_api_key_sid and settings.twilio_api_key_secret and self.twiml_app_sid):
            raise TelephonyError(
                "Browser calling needs TWILIO_API_KEY_SID/TWILIO_API_KEY_SECRET and a TwiML app (run setup-agent)"
            )
        token = AccessToken(
            settings.twilio_account_sid,
            settings.twilio_api_key_sid,
            settings.twilio_api_key_secret.get_secret_value(),
            identity=identity,
            ttl=ttl,
            region=settings.twilio_region,
        )
        token.add_grant(VoiceGrant(outgoing_application_sid=self.twiml_app_sid, incoming_allow=False))
        jwt = token.to_jwt()
        return jwt.decode() if isinstance(jwt, bytes) else jwt

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        if not self._settings.twilio_validate_signatures:
            return True
        if not signature or self._settings.twilio_auth_token is None:
            return False
        from twilio.request_validator import RequestValidator

        return RequestValidator(self._settings.twilio_auth_token.get_secret_value()).validate(url, params, signature)

    # --- setup helper (used by `python -m app.cli setup-agent`) ---------------------------

    def ensure_twiml_app(self, voice_url: str, existing_sid: str | None) -> str:
        """Create or update the TwiML app that browser calls use. Synchronous (CLI only)."""
        if existing_sid:
            try:
                self._client.applications(existing_sid).update(voice_url=voice_url, voice_method="POST")
                return existing_sid
            except Exception:
                log.info("TwiML app %s not found - creating a new one", existing_sid)
        app = self._client.applications.create(
            friendly_name="Health AI browser calls", voice_url=voice_url, voice_method="POST"
        )
        return app.sid


def build_telephony(settings: Settings, twiml_app_sid: str | None = None) -> Telephony:
    if not settings.telephony_configured:
        return DisabledTelephony()
    telephony = TwilioTelephony(settings, twiml_app_sid)
    log.info("Twilio telephony enabled (number %s)", telephony.from_number)
    return telephony
