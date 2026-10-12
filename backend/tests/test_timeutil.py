from datetime import UTC, date, datetime

from app import timeutil


def test_ordinals() -> None:
    expected = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th", 21: "21st", 22: "22nd", 23: "23rd", 31: "31st"}
    for n, text in expected.items():
        assert timeutil.ordinal(n) == text


def test_speak_time_uses_clinic_local_time() -> None:
    # 13 Oct 2026 is BST (UTC+1)
    assert timeutil.speak_time(datetime(2026, 10, 13, 8, 0, tzinfo=UTC)) == "9am"
    assert timeutil.speak_time(datetime(2026, 10, 13, 13, 30, tzinfo=UTC)) == "2:30pm"
    assert timeutil.speak_time(datetime(2026, 10, 13, 11, 0, tzinfo=UTC)) == "12pm"
    # 2 Nov 2026 is GMT (UTC+0)
    assert timeutil.speak_time(datetime(2026, 11, 2, 9, 0, tzinfo=UTC)) == "9am"


def test_speak_date_adds_year_only_when_different() -> None:
    assert timeutil.speak_date(date(2026, 10, 14), today=date(2026, 10, 12)) == "Wednesday 14th October"
    assert timeutil.speak_date(date(2027, 1, 4), today=date(2026, 10, 12)) == "Monday 4th January 2027"


def test_parse_instant_treats_naive_values_as_london_time() -> None:
    assert timeutil.parse_instant("2026-07-01T09:00") == datetime(2026, 7, 1, 8, 0, tzinfo=UTC)  # BST
    assert timeutil.parse_instant("2026-12-01T09:00") == datetime(2026, 12, 1, 9, 0, tzinfo=UTC)  # GMT
    assert timeutil.parse_instant("2026-07-01T09:00:00+01:00") == datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    assert timeutil.parse_instant("2026-07-01T08:00:00Z") == datetime(2026, 7, 1, 8, 0, tzinfo=UTC)


def test_iso_local_round_trips() -> None:
    instant = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    assert timeutil.iso_local(instant) == "2026-07-01T09:00+01:00"
    assert timeutil.parse_instant(timeutil.iso_local(instant)) == instant


def test_format_price() -> None:
    assert timeutil.format_price(9500) == "£95"
    assert timeutil.format_price(4550) == "£45.50"
