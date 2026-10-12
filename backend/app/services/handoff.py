"""Hand a conversation to a person at reception.

* Phone: the live call is redirected from the AI to the reception number. If nobody
  answers, the caller is told and a callback request is created automatically.
* Web: the browser places a call to reception (Twilio Voice SDK) via the TwiML app.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_settings
from ..models import VoiceSession
from .clinic_hours import clinic_open_now
from .telephony import Telephony, TelephonyError, to_e164, twiml_dial_number

log = logging.getLogger(__name__)

TRANSFER_INTRO = "Putting you through to our reception team now."
# Pause before transferring, so the agent can finish its sentence.
HANDOVER_DELAY_SECONDS = 2.0
NO_ANSWER_MESSAGE = (
    "Sorry, nobody at reception could answer just now. We've asked the team to call you back as soon as possible. Goodbye."
)


async def reception_unavailable_reason(db: AsyncSession, telephony: Telephony) -> str | None:
    """None if a call can be put through to reception now; otherwise why not."""
    settings = get_settings()
    if not settings.reception_phone_number or not telephony.enabled:
        return "not_configured"
    if not await clinic_open_now(db):
        return "closed"
    return None


def after_dial_url(session_id: uuid.UUID) -> str:
    return f"{get_settings().public_base_url}/twilio/reception/{session_id}/after-dial"


def reception_twiml(telephony: Telephony, session_id: uuid.UUID, intro: str | None = TRANSFER_INTRO) -> str:
    settings = get_settings()
    return twiml_dial_number(
        to_e164(settings.reception_phone_number or ""),
        caller_id=telephony.from_number or "",
        action_url=after_dial_url(session_id),
        intro=intro,
    )


async def transfer_call_to_reception(
    sessionmaker: async_sessionmaker[AsyncSession], telephony: Telephony, session_id: uuid.UUID
) -> None:
    """Background task: let the agent finish its sentence, then move the phone call to reception."""
    await asyncio.sleep(HANDOVER_DELAY_SECONDS)
    async with sessionmaker() as db:
        session = await db.get(VoiceSession, session_id)
        if session is None or not session.call_sid:
            return
        try:
            await telephony.update_call(session.call_sid, reception_twiml(telephony, session.id))
        except TelephonyError as exc:
            log.error("Could not transfer call %s to reception: %s", session.call_sid, exc)
