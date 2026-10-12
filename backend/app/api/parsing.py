"""Lenient parsing for values that come from an LLM listening to speech."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
)


def parse_flexible_date(value: Any) -> Any:
    """Accept ISO dates plus common UK spellings ("12/04/1990", "12th April 1990")."""
    if value is None or isinstance(value, date):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    # "12th April 1990" -> "12 April 1990"; "April 12, 1990" -> "April 12 1990"
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE).replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return value  # let pydantic report a clear error


def blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "any", "no preference"}:
        return None
    return value
