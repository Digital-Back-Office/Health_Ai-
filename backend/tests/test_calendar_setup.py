from pathlib import Path

import pytest

from app.config import Settings
from app.services.calendar import GoogleCalendar, explain_calendar_error, plan_calendar_links

PREFIX = "Health AI - "
CALENDARS = [
    {"id": "patel@group.calendar.google.com", "summary": "Health AI - Dr Sarah Patel", "access_role": "owner"},
    {"id": "clarke@group.calendar.google.com", "summary": "emma clarke", "access_role": "writer"},
    {"id": "me@example.com", "summary": "me@example.com", "access_role": "owner"},
]


def test_plan_keeps_links_matches_names_and_creates_the_rest() -> None:
    clinicians = [
        (1, "Dr Sarah Patel", None),  # matches "Health AI - Dr Sarah Patel"
        (2, "Dr James Whitfield", None),  # nothing matches -> create
        (3, "Emma Clarke", None),  # matches the plain name, case-insensitively
        (4, "Olivia Reid", "me@example.com"),  # already linked to a writable calendar -> keep
    ]
    plan = {step.clinician_id: step for step in plan_calendar_links(clinicians, CALENDARS, PREFIX)}
    assert (plan[1].action, plan[1].calendar_id) == ("link", "patel@group.calendar.google.com")
    assert (plan[2].action, plan[2].calendar_name, plan[2].calendar_id) == ("create", "Health AI - Dr James Whitfield", None)
    assert (plan[3].action, plan[3].calendar_id) == ("link", "clarke@group.calendar.google.com")
    assert (plan[4].action, plan[4].calendar_id) == ("keep", "me@example.com")


def test_plan_relinks_when_the_linked_calendar_is_no_longer_writable() -> None:
    plan = plan_calendar_links([(1, "Dr Sarah Patel", "gone@group.calendar.google.com")], CALENDARS, PREFIX)
    assert (plan[0].action, plan[0].calendar_id) == ("link", "patel@group.calendar.google.com")


def test_missing_service_account_file_gives_a_clear_error(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, google_service_account_file=tmp_path / "nope.json")
    with pytest.raises(RuntimeError, match="Leave it empty to use your gcloud login"):
        GoogleCalendar.from_settings(settings)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Google Calendar API error 403: Request had insufficient authentication scopes.", "gcloud auth application-default login"),
        ("Google Calendar API error 403: Google Calendar API has not been used in project 123 before or it is disabled", "Enable the Google Calendar API"),
        ("Google Calendar API error 404: Not Found", "Calendar not found"),
    ],
)
def test_error_hints(message: str, expected: str) -> None:
    hint = explain_calendar_error(message, "dataflow-zone1")
    assert hint is not None and expected in hint
