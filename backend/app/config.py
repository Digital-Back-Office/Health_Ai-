"""Application settings, loaded from environment variables / backend/.env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEV_SECRET_PREFIX = "dev-insecure-"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # APP_ENV_FILE="" disables .env loading (the test suite uses this).
        env_file=os.environ.get("APP_ENV_FILE", str(BACKEND_DIR / ".env")) or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application -------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    # Public HTTPS URL that ElevenLabs' servers use to reach this backend
    # (tool webhooks + the custom LLM proxy). In development this is a tunnel.
    public_base_url: str = "http://localhost:8000"
    frontend_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://healthcare:healthcare@localhost:5433/healthcare"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- Clinic --------------------------------------------------------------
    clinic_name: str = "Health AI"
    clinic_timezone: str = "Europe/London"
    clinic_phone: str = "020 7946 0000"
    clinic_email: str = "hello@healthai.example"
    clinic_address: str = "14 Riverside Walk, London SE1 9QQ"
    assistant_name: str = "Sophie"

    # --- Scheduling rules ---------------------------------------------------
    slot_step_minutes: int = 15
    min_notice_minutes: int = 120
    booking_horizon_days: int = 60
    default_search_days: int = 14
    max_search_days: int = 31
    max_slots_returned: int = 6
    max_slots_per_day: int = 3
    late_cancellation_hours: int = 24
    max_upcoming_appointments_per_patient: int = 5

    # --- Sessions & security -------------------------------------------------
    tool_secret: SecretStr = SecretStr(DEV_SECRET_PREFIX + "tool-secret")
    llm_proxy_secret: SecretStr = SecretStr(DEV_SECRET_PREFIX + "llm-proxy-secret")
    admin_token: SecretStr = SecretStr(DEV_SECRET_PREFIX + "admin-token")
    session_ttl_minutes: int = 30
    session_idle_seconds: int = 90
    max_verification_attempts: int = 3
    # Should match the concurrency of your ElevenLabs plan (Free = 4).
    max_concurrent_sessions: int = 4
    session_starts_per_minute_per_ip: int = 6

    # --- ElevenLabs ----------------------------------------------------------
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_agent_id: str | None = None
    elevenlabs_api_base: str = "https://api.elevenlabs.io"
    elevenlabs_webhook_secret: SecretStr | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_tts_model: str = "eleven_flash_v2"
    elevenlabs_daily_conversation_limit: int = 200
    # Dynamic variable that carries the per-session token to tool webhooks.
    # Normally chosen by `setup-agent` ("secret__session_token": usable only in
    # headers and never shown to the LLM); set it here only to override.
    elevenlabs_session_token_var: str | None = None
    store_transcripts: bool = False

    # --- Google Calendar -----------------------------------------------------
    google_calendar_enabled: bool = False
    google_service_account_file: Path | None = None
    google_impersonate_user: str | None = None
    calendar_resync_interval_seconds: int = 60

    # --- Vertex AI (custom LLM) ---------------------------------------------
    vertex_project_id: str | None = None
    vertex_location: str = "europe-west2"
    vertex_model: str = "google/gemini-3.5-flash"
    # "minimal" keeps voice turns around a second; "low" adds roughly 2 s of thinking.
    vertex_reasoning_effort: str | None = "minimal"
    vertex_credentials_file: Path | None = None
    vertex_timeout_seconds: float = 30.0
    # Gemini 3 is tuned for its default temperature (1.0); drop the agent's value.
    vertex_drop_temperature: bool = True

    @field_validator("frontend_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator(
        "elevenlabs_agent_id",
        "elevenlabs_voice_id",
        "vertex_project_id",
        "google_impersonate_user",
        "vertex_reasoning_effort",
        # Blank paths must mean "not set", not Path("") == Path(".").
        "vertex_credentials_file",
        "google_service_account_file",
        "elevenlabs_api_key",
        "elevenlabs_webhook_secret",
        "elevenlabs_session_token_var",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _refuse_dev_secrets_in_production(self) -> Settings:
        if self.environment == "production":
            for name in self.placeholder_secrets():
                raise ValueError(f"{name.upper()} must be set to a strong value in production")
        return self

    def placeholder_secrets(self) -> list[str]:
        """Names of shared secrets still set to a development/example placeholder."""
        from .envfile import is_placeholder_secret

        return [
            name
            for name in ("tool_secret", "llm_proxy_secret", "admin_token")
            if is_placeholder_secret(getattr(self, name).get_secret_value())
        ]

    @property
    def asyncpg_dsn(self) -> str:
        """DSN for raw asyncpg connections (LISTEN/NOTIFY)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def vertex_base_url(self) -> str:
        host = (
            "aiplatform.googleapis.com"
            if self.vertex_location == "global"
            else f"{self.vertex_location}-aiplatform.googleapis.com"
        )
        return (
            f"https://{host}/v1/projects/{self.vertex_project_id}"
            f"/locations/{self.vertex_location}/endpoints/openapi"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
