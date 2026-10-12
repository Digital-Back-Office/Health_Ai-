"""Small helpers for reading and updating backend/.env without disturbing other lines."""

from __future__ import annotations

from pathlib import Path

# Values that must never be used while the backend is reachable from the internet.
PLACEHOLDER_SECRETS = ("change-me", "dev-insecure-")


def is_placeholder_secret(value: str) -> bool:
    return not value or value.startswith(PLACEHOLDER_SECRETS)


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines (replacing existing ones, appending new ones); UTF-8, no BOM."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
