import jwt
import pytest
from twilio.request_validator import RequestValidator

from app.config import Settings
from app.services.telephony import (
    TelephonyError,
    TwilioTelephony,
    from_e164_uk,
    to_e164,
    twiml_dial_number,
    twiml_doctor_join,
    twiml_doctor_prompt,
    twiml_patient_conference,
)

AUTH_TOKEN = "test-twilio-auth-token"


def _twilio_settings(**overrides: object) -> Settings:
    values = {
        "twilio_account_sid": "AC" + "1" * 32,
        "twilio_auth_token": AUTH_TOKEN,
        "twilio_api_key_sid": "SK" + "2" * 32,
        "twilio_api_key_secret": "test-api-key-secret-that-is-long-enough",
        "twilio_phone_number": "+442079460000",
        "twilio_twiml_app_sid": "AP" + "3" * 32,
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07700 900123", "+447700900123"),
        ("+44 7700 900123", "+447700900123"),
        ("0044 7700 900123", "+447700900123"),
        ("020 7946 0000", "+442079460000"),
        ("+1 415 555 0100", "+14155550100"),
    ],
)
def test_to_e164(raw: str, expected: str) -> None:
    assert to_e164(raw) == expected
    assert from_e164_uk(expected).startswith("0") == expected.startswith("+44")


@pytest.mark.parametrize("raw", ["", "12", "anonymous"])
def test_to_e164_rejects_junk(raw: str) -> None:
    with pytest.raises(TelephonyError):
        to_e164(raw)


def test_patient_waits_and_ends_the_call_for_everyone() -> None:
    xml = twiml_patient_conference("sos-abc", "https://x.test/conf", "Connecting you now.")
    assert 'startConferenceOnEnter="false"' in xml and 'endConferenceOnExit="true"' in xml
    assert ">sos-abc</Conference>" in xml and "Connecting you now." in xml


def test_doctor_must_press_1_and_joins_without_ending_the_call() -> None:
    prompt = twiml_doctor_prompt("https://x.test/accept?call=1", "Patient Alice & Bob <test>.")
    assert '<Gather numDigits="1"' in prompt and 'action="https://x.test/accept?call=1"' in prompt
    assert "Alice &amp; Bob &lt;test&gt;" in prompt  # escaped
    join = twiml_doctor_join("sos-abc", "https://x.test/conf")
    assert 'startConferenceOnEnter="true"' in join and 'endConferenceOnExit="false"' in join


def test_dial_number_uses_clinic_caller_id() -> None:
    xml = twiml_dial_number("+442070000000", "+442079460000", "https://x.test/after")
    assert 'callerId="+442079460000"' in xml and "<Number>+442070000000</Number>" in xml


def test_webhook_signatures_are_checked() -> None:
    telephony = TwilioTelephony(_twilio_settings())
    url = "https://example.ngrok-free.app/twilio/sos/123/doctor-accept?call=4"
    params = {"Digits": "1", "CallSid": "CA123"}
    good = RequestValidator(AUTH_TOKEN).compute_signature(url, params)
    assert telephony.validate_request(url, params, good)
    assert not telephony.validate_request(url, {**params, "Digits": "2"}, good)
    assert not telephony.validate_request(url, params, None)
    relaxed = TwilioTelephony(_twilio_settings(twilio_validate_signatures=False))
    assert relaxed.validate_request(url, params, None)


def test_browser_token_grants_outgoing_calls_only() -> None:
    telephony = TwilioTelephony(_twilio_settings())
    token = telephony.browser_token("web-abc", ttl=600)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["grants"]["identity"] == "web-abc"
    voice = claims["grants"]["voice"]
    assert voice["outgoing"]["application_sid"].startswith("AP")
    assert not voice.get("incoming", {}).get("allow", False)


def test_browser_token_needs_an_api_key() -> None:
    telephony = TwilioTelephony(_twilio_settings(twilio_api_key_sid=None, twilio_api_key_secret=None))
    with pytest.raises(TelephonyError):
        telephony.browser_token("web-abc")


def test_telephony_configured_flag() -> None:
    assert _twilio_settings().telephony_configured
    assert not Settings(_env_file=None).telephony_configured
    assert not _twilio_settings(twilio_phone_number=None).telephony_configured


def test_browser_token_picks_up_a_twiml_app_created_later(tmp_path, monkeypatch) -> None:
    from app.services import elevenlabs as el

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(el, "STATE_FILE", state_file)
    telephony = TwilioTelephony(_twilio_settings(twilio_twiml_app_sid=None))
    with pytest.raises(TelephonyError):
        telephony.browser_token("web-abc")
    el.save_state({"twiml_app_sid": "AP" + "4" * 32}, state_file)  # setup-agent ran meanwhile
    claims = jwt.decode(telephony.browser_token("web-abc"), options={"verify_signature": False})
    assert claims["grants"]["voice"]["outgoing"]["application_sid"] == "AP" + "4" * 32


def test_backend_follows_a_new_tunnel_url(tmp_path, monkeypatch) -> None:
    from app.config import refresh_public_base_url

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("PUBLIC_BASE_URL=https://old.ngrok-free.app\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.public_base_url == "https://old.ngrok-free.app"
    assert refresh_public_base_url(settings, env_file) is None  # unchanged

    env_file.write_text("PUBLIC_BASE_URL=https://new.ngrok-free.app/\n", encoding="utf-8")  # dev.ps1 rewrote it
    assert refresh_public_base_url(settings, env_file) == "https://new.ngrok-free.app"
    assert settings.public_base_url == "https://new.ngrok-free.app"

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pinned.example")  # a real env var always wins
    env_file.write_text("PUBLIC_BASE_URL=https://other.ngrok-free.app\n", encoding="utf-8")
    assert refresh_public_base_url(settings, env_file) is None
