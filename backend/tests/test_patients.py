from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import patients
from app.services import sessions as session_service


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ec1a 1bb", "EC1A1BB"), (" M1 1AE ", "M11AE"), ("CR2 6XH", "CR26XH"), ("sw1a1aa", "SW1A1AA")],
)
def test_normalise_postcode(raw: str, expected: str) -> None:
    assert patients.normalise_postcode(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "ABC", "", "EC1A 1B"])
def test_invalid_postcodes(raw: str) -> None:
    with pytest.raises(patients.PatientInputError):
        patients.normalise_postcode(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("07700 900123", "07700900123"), ("+44 7700 900123", "07700900123"), ("020 7946 0000", "02079460000")],
)
def test_normalise_phone(raw: str, expected: str) -> None:
    assert patients.normalise_phone(raw) == expected


def test_name_key_ignores_case_accents_and_punctuation() -> None:
    assert patients.name_key("O'Brien") == patients.name_key("obrien")
    assert patients.name_key("Smith-Jones") == patients.name_key("smith jones")
    assert patients.name_key("Zoë") == patients.name_key("zoe")


async def test_verify_patient_success_is_case_insensitive(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    result = await patients.verify_patient(
        db, session, last_name="testington", date_of_birth=date(1990, 4, 12), phone="+44 7700 900123"
    )
    assert result.verified and result.patient.first_name == "Alice"
    assert session.verified_at is not None and session.patient_id == result.patient.id


async def test_verification_locks_after_three_failures(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    for expected_remaining in (2, 1, 0):
        result = await patients.verify_patient(
            db, session, last_name="Testington", date_of_birth=date(1991, 4, 12), phone="07700 900123"
        )
        assert not result.verified and result.attempts_remaining == expected_remaining
    assert result.locked
    # Even correct details are refused once locked.
    result = await patients.verify_patient(
        db, session, last_name="Testington", date_of_birth=date(1990, 4, 12), phone="07700 900123"
    )
    assert not result.verified and result.locked


async def test_register_new_patient(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    result = await patients.register_patient(
        db,
        session,
        first_name="Dan",
        last_name="Newman",
        date_of_birth=date(1979, 2, 28),
        phone="07700 900999",
    )
    assert not result.already_registered
    assert result.patient.postcode is None and result.patient.phone == "07700900999"
    assert session.patient_id == result.patient.id


async def test_register_existing_patient_verifies_instead(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    result = await patients.register_patient(
        db,
        session,
        first_name="Alice",
        last_name="TESTINGTON",
        date_of_birth=date(1990, 4, 12),
        phone="07700 900123",
    )
    assert result.already_registered and result.patient.first_name == "Alice"


async def test_register_rejects_future_date_of_birth(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    with pytest.raises(patients.PatientInputError):
        await patients.register_patient(
            db, session, first_name="A", last_name="B", date_of_birth=date(2999, 1, 1), phone="07700900111"
        )


async def test_verification_needs_the_phone_on_record(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    wrong_phone = await patients.verify_patient(
        db, session, last_name="Testington", date_of_birth=date(1990, 4, 12), phone="07700 900999"
    )
    assert not wrong_phone.verified
    with pytest.raises(patients.PatientInputError):
        await patients.verify_patient(db, session, last_name="Testington", date_of_birth=date(1990, 4, 12), phone="12")


async def test_register_accepts_optional_postcode(db: AsyncSession) -> None:
    session, _ = await session_service.create_session(db)
    result = await patients.register_patient(
        db, session, first_name="Eve", last_name="Postcode", date_of_birth=date(1970, 1, 1), phone="07700900333", postcode="b33 8th"
    )
    assert result.patient.postcode == "B338TH"
