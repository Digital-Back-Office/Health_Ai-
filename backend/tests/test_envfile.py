from pathlib import Path

import pytest

from app.config import Settings
from app.envfile import is_placeholder_secret, update_env_file

STRONG = "Zq4mR7tV9yB2nK5pX8sD1fG3hJ6lW0cE"


def test_update_env_file_replaces_and_appends(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nA=1\nTOOL_SECRET=change-me-random\nCLINIC_NAME=Health AI\n# TOOL_SECRET=commented-out\n",
        encoding="utf-8",
    )
    update_env_file(env, {"TOOL_SECRET": "abc", "NEW_KEY": "x"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "TOOL_SECRET=abc" in lines and "TOOL_SECRET=change-me-random" not in lines
    assert "# TOOL_SECRET=commented-out" in lines  # comments are left alone
    assert "CLINIC_NAME=Health AI" in lines and "A=1" in lines
    assert lines[-1] == "NEW_KEY=x"
    assert not env.read_bytes().startswith(b"\xef\xbb\xbf")  # no BOM


def test_placeholder_detection() -> None:
    assert is_placeholder_secret("change-me-random")
    assert is_placeholder_secret("dev-insecure-tool-secret")
    assert is_placeholder_secret("")
    assert not is_placeholder_secret(STRONG)
    settings = Settings(_env_file=None, tool_secret="change-me-random", llm_proxy_secret=STRONG, admin_token="dev-insecure-admin-token")
    assert settings.placeholder_secrets() == ["tool_secret", "admin_token"]


def test_production_refuses_placeholder_secrets() -> None:
    with pytest.raises(ValueError, match="TOOL_SECRET"):
        Settings(_env_file=None, environment="production", tool_secret="change-me-random", llm_proxy_secret=STRONG, admin_token=STRONG)
    ok = Settings(_env_file=None, environment="production", tool_secret=STRONG, llm_proxy_secret=STRONG, admin_token=STRONG)
    assert ok.placeholder_secrets() == []
