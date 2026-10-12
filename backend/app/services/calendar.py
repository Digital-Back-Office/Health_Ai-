"""Google Calendar gateway.

Each clinician has their own Google calendar. Credentials, in order:

* GOOGLE_SERVICE_ACCOUNT_FILE set: that service account. Clinicians' calendars
  are shared with it ("Make changes to events"), or it impersonates a Workspace
  user via domain-wide delegation (GOOGLE_IMPERSONATE_USER).
* otherwise Application Default Credentials: your own gcloud login during
  development (it needs the Calendar scope, see CALENDAR_LOGIN_COMMAND), or the
  attached service account when running on Google Cloud (no key file needed).

* busy_intervals() feeds staff blocks (leave, meetings, lunch) into availability.
* upsert_event()/delete_event() mirror bookings so staff see them in Google
  Calendar. Event ids are derived from appointment ids, so every write is
  idempotent and can be retried safely.

Only minimal details go into calendar events - no symptoms or clinical notes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .. import timeutil

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
# Keeps cloud-platform too, so the same login still works for Vertex AI.
CALENDAR_LOGIN_COMMAND = (
    "gcloud auth application-default login --scopes="
    "https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/cloud-platform"
)

BusyMap = dict[str, list[tuple[datetime, datetime]]]


class CalendarError(Exception):
    """A calendar operation failed (network, permissions, quota...)."""


@dataclass(frozen=True)
class CalendarEvent:
    summary: str
    description: str
    starts_at: datetime
    ends_at: datetime
    private_properties: dict[str, str] = field(default_factory=dict)


class CalendarGateway(Protocol):
    enabled: bool

    async def busy_intervals(self, calendar_ids: Sequence[str], start: datetime, end: datetime) -> BusyMap: ...

    async def upsert_event(self, calendar_id: str, event_id: str, event: CalendarEvent) -> None: ...

    async def delete_event(self, calendar_id: str, event_id: str) -> None: ...


class DisabledCalendar:
    """Used when Google Calendar integration is switched off."""

    enabled = False

    async def busy_intervals(self, calendar_ids: Sequence[str], start: datetime, end: datetime) -> BusyMap:
        return {}

    async def upsert_event(self, calendar_id: str, event_id: str, event: CalendarEvent) -> None:
        return None

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        return None


def describe_credentials(credentials: Any) -> str:
    """Human-readable identity for logs and the CLI (never includes secrets)."""
    email = getattr(credentials, "service_account_email", None)
    if email and email != "default":
        return f"service account {email}"
    if type(credentials).__module__ == "google.oauth2.credentials":
        account = getattr(credentials, "account", "") or ""
        return f"your gcloud login{f' {account}' if account else ''} (application default credentials)"
    return f"application default credentials ({type(credentials).__name__})"


def explain_calendar_error(message: str, project: str | None = None) -> str | None:
    """A fix-it hint for the most common Google Calendar setup errors."""
    lower = message.lower()
    if "insufficient" in lower and "scope" in lower:
        return f"Your Google login is missing the Calendar permission. Run:\n    {CALENDAR_LOGIN_COMMAND}"
    if "has not been used" in lower or "is disabled" in lower or "accessnotconfigured" in lower:
        url = "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"
        return f"Enable the Google Calendar API for your project: {url}{f'?project={project}' if project else ''}"
    if "error 404" in lower or "notfound" in lower:
        return "Calendar not found - check the calendar id, and that it's shared with this identity."
    if "error 403" in lower:
        return "Permission denied - share the calendar with this identity ('Make changes to events')."
    return None


class GoogleCalendar:
    enabled = True

    def __init__(self, credentials: Any) -> None:
        # Imported lazily so the app (and tests) don't need Google libraries loaded
        # when the integration is disabled.
        from googleapiclient.discovery import build

        self._credentials = credentials
        self.identity = describe_credentials(credentials)
        self.quota_project: str | None = getattr(credentials, "quota_project_id", None)
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @classmethod
    def from_settings(cls, settings: Any) -> GoogleCalendar:
        if settings.google_service_account_file:
            path = Path(settings.google_service_account_file)
            if not path.exists():
                raise RuntimeError(
                    f"GOOGLE_SERVICE_ACCOUNT_FILE '{path}' does not exist. Leave it empty to use your gcloud login."
                )
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
            if settings.google_impersonate_user:
                credentials = credentials.with_subject(settings.google_impersonate_user)
            return cls(credentials)

        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        try:
            credentials, _project = google.auth.default(scopes=SCOPES)
        except DefaultCredentialsError as exc:
            raise RuntimeError(
                "No Google credentials found for Calendar. Run\n"
                f"    {CALENDAR_LOGIN_COMMAND}\nor set GOOGLE_SERVICE_ACCOUNT_FILE."
            ) from exc
        return cls(credentials)

    async def _execute(self, request: Any) -> Any:
        """Run a googleapiclient request in a worker thread.

        httplib2 is not thread-safe, so each call gets its own authorised Http.
        """
        import google_auth_httplib2
        import httplib2
        from googleapiclient.errors import HttpError

        http = google_auth_httplib2.AuthorizedHttp(self._credentials, http=httplib2.Http(timeout=15))
        try:
            return await asyncio.to_thread(request.execute, http=http, num_retries=2)
        except HttpError as exc:
            raise CalendarError(f"Google Calendar API error {exc.status_code}: {exc.reason}") from exc
        except Exception as exc:  # network errors, auth errors...
            raise CalendarError(f"Google Calendar request failed: {exc}") from exc

    async def busy_intervals(self, calendar_ids: Sequence[str], start: datetime, end: datetime) -> BusyMap:
        if not calendar_ids:
            return {}
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": "UTC",
            "items": [{"id": calendar_id} for calendar_id in calendar_ids],
        }
        response = await self._execute(self._service.freebusy().query(body=body))
        busy: BusyMap = {}
        for calendar_id, data in response.get("calendars", {}).items():
            if data.get("errors"):
                # Fail closed: if we can't see a clinician's calendar we must not
                # offer times that might clash with their leave or meetings.
                reasons = ", ".join(err.get("reason", "unknown") for err in data["errors"])
                raise CalendarError(f"Cannot read free/busy for calendar {calendar_id}: {reasons}")
            busy[calendar_id] = [
                (timeutil.parse_instant(block["start"]), timeutil.parse_instant(block["end"]))
                for block in data.get("busy", [])
            ]
        return busy

    def _event_body(self, event_id: str, event: CalendarEvent) -> dict[str, Any]:
        tz = timeutil.clinic_tz().key
        return {
            "id": event_id,
            "status": "confirmed",
            "summary": event.summary,
            "description": event.description,
            "start": {"dateTime": timeutil.to_clinic(event.starts_at).isoformat(), "timeZone": tz},
            "end": {"dateTime": timeutil.to_clinic(event.ends_at).isoformat(), "timeZone": tz},
            "transparency": "opaque",
            "extendedProperties": {"private": {"source": "ai-receptionist", **event.private_properties}},
        }

    async def upsert_event(self, calendar_id: str, event_id: str, event: CalendarEvent) -> None:
        body = self._event_body(event_id, event)
        events = self._service.events()
        try:
            await self._execute(events.insert(calendarId=calendar_id, body=body, sendUpdates="none"))
        except CalendarError as exc:
            # 409: the id already exists (a retry, a reschedule, or a previously
            # deleted event) - update it in place, which also "undeletes" it.
            if "error 409" not in str(exc):
                raise
            await self._execute(
                events.update(calendarId=calendar_id, eventId=event_id, body=body, sendUpdates="none")
            )

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        try:
            await self._execute(
                self._service.events().delete(calendarId=calendar_id, eventId=event_id, sendUpdates="none")
            )
        except CalendarError as exc:
            if "error 404" in str(exc) or "error 410" in str(exc):
                return  # already gone
            raise

    # --- setup helpers (used by `python -m app.cli setup-calendars`) ---------------

    async def list_calendars(self) -> list[dict[str, str]]:
        """Calendars this identity can write to."""
        calendars: list[dict[str, str]] = []
        page_token = None
        while True:
            response = await self._execute(
                self._service.calendarList().list(minAccessRole="writer", maxResults=250, pageToken=page_token)
            )
            for item in response.get("items", []):
                calendars.append(
                    {
                        "id": item["id"],
                        "summary": item.get("summaryOverride") or item.get("summary", ""),
                        "access_role": item.get("accessRole", ""),
                    }
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                return calendars

    async def create_calendar(self, summary: str, description: str = "") -> str:
        created = await self._execute(
            self._service.calendars().insert(
                body={"summary": summary, "description": description, "timeZone": timeutil.clinic_tz().key}
            )
        )
        return created["id"]

    async def share_calendar(self, calendar_id: str, email: str, role: str = "writer") -> None:
        await self._execute(
            self._service.acl().insert(
                calendarId=calendar_id,
                body={"role": role, "scope": {"type": "user", "value": email}},
                sendNotifications=False,
            )
        )


def event_id_for(appointment_id: Any) -> str:
    """Google event ids allow [a-v0-9]; a UUID's hex form fits."""
    return f"hc{appointment_id.hex}"


def build_calendar_gateway() -> CalendarGateway:
    from ..config import get_settings

    settings = get_settings()
    if not settings.google_calendar_enabled:
        return DisabledCalendar()
    gateway = GoogleCalendar.from_settings(settings)
    log.info("Google Calendar enabled (%s)", gateway.identity)
    return gateway


# --- linking clinicians to calendars --------------------------------------------------


@dataclass
class CalendarLink:
    clinician_id: int
    clinician_name: str
    action: str  # "keep" | "link" | "create"
    calendar_name: str
    calendar_id: str | None = None


def plan_calendar_links(
    clinicians: Sequence[tuple[int, str, str | None]], calendars: Sequence[dict[str, str]], name_prefix: str
) -> list[CalendarLink]:
    """Decide, per clinician, whether to keep the linked calendar, link an existing
    one with a matching name, or create a new "<prefix><clinician>" calendar."""
    writable = {c["id"]: c for c in calendars}
    by_name = {c["summary"].strip().casefold(): c for c in calendars}
    plan: list[CalendarLink] = []
    for clinician_id, name, current_id in clinicians:
        wanted = f"{name_prefix}{name}"
        if current_id and current_id in writable:
            plan.append(CalendarLink(clinician_id, name, "keep", writable[current_id]["summary"], current_id))
            continue
        match = by_name.get(wanted.casefold()) or by_name.get(name.casefold())
        if match:
            plan.append(CalendarLink(clinician_id, name, "link", match["summary"], match["id"]))
        else:
            plan.append(CalendarLink(clinician_id, name, "create", wanted))
    return plan
