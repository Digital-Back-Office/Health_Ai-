from app.config import Settings


def test_blank_optional_values_mean_not_set(monkeypatch) -> None:
    for name in (
        "VERTEX_CREDENTIALS_FILE",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "ELEVENLABS_WEBHOOK_SECRET",
        "ELEVENLABS_AGENT_ID",
        "ELEVENLABS_SESSION_TOKEN_VAR",
    ):
        monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)
    assert settings.vertex_credentials_file is None  # i.e. use application default credentials
    assert settings.google_service_account_file is None
    assert settings.elevenlabs_webhook_secret is None
    assert settings.elevenlabs_agent_id is None
    assert settings.elevenlabs_session_token_var is None


def test_vertex_base_url() -> None:
    regional = Settings(_env_file=None, vertex_project_id="p1", vertex_location="europe-west2")
    assert regional.vertex_base_url == (
        "https://europe-west2-aiplatform.googleapis.com/v1/projects/p1/locations/europe-west2/endpoints/openapi"
    )
    global_ = Settings(_env_file=None, vertex_project_id="p1", vertex_location="global")
    assert global_.vertex_base_url.startswith("https://aiplatform.googleapis.com/v1/projects/p1/locations/global/")
