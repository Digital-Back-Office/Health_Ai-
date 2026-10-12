"""Patient identification (verify returning patients, register new ones)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import timeutil
from ..config import get_settings
from ..models import Patient, VoiceSession

UK_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}$")
MINOR_AGE = 16


class PatientInputError(ValueError):
    """Details given by the caller are not valid (bad postcode, phone, date...)."""


def normalise_postcode(value: str) -> str:
    compact = re.sub(r"\s+", "", value or "").upper()
    if not UK_POSTCODE_RE.match(compact):
        raise PatientInputError(f"'{value}' is not a valid UK postcode")
    return compact


def format_postcode(compact: str) -> str:
    return f"{compact[:-3]} {compact[-3:]}"


def normalise_phone(value: str) -> str:
    digits = re.sub(r"[^\d+]", "", value or "")
    if digits.startswith("+44"):
        digits = "0" + digits[3:]
    elif digits.startswith("0044"):
        digits = "0" + digits[4:]
    if not re.fullmatch(r"0\d{9,10}", digits):
        raise PatientInputError(f"'{value}' is not a valid UK phone number")
    return digits


def name_key(value: str) -> str:
    """Case/accents/punctuation-insensitive comparison key for surnames."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    letters = "".join(ch for ch in decomposed if ch.isalpha())
    return letters.casefold()


def clean_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    if not cleaned or len(cleaned) > 80 or not any(ch.isalpha() for ch in cleaned):
        raise PatientInputError("Please provide a valid name")
    return cleaned


def age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_dob(dob: date) -> None:
    today = timeutil.clinic_today()
    if dob > today:
        raise PatientInputError("Date of birth cannot be in the future")
    if age_on(dob, today) > 120:
        raise PatientInputError("Please check the date of birth")


@dataclass
class VerificationResult:
    verified: bool
    patient: Patient | None = None
    attempts_remaining: int = 0
    locked: bool = False


def is_locked(session: VoiceSession) -> bool:
    return session.failed_verifications >= get_settings().max_verification_attempts


async def _find_matching(db: AsyncSession, last_name: str, dob: date, phone: str) -> list[Patient]:
    """Identity = surname + date of birth + the phone number on the record."""
    candidates = (
        await db.execute(select(Patient).where(Patient.date_of_birth == dob, Patient.phone == phone))
    ).scalars()
    key = name_key(last_name)
    return [patient for patient in candidates if name_key(patient.last_name) == key]


def _bind(session: VoiceSession, patient: Patient) -> None:
    session.patient_id = patient.id
    session.patient = patient
    session.verified_at = timeutil.now_utc()
    session.failed_verifications = 0


async def verify_patient(
    db: AsyncSession, session: VoiceSession, *, last_name: str, date_of_birth: date, phone: str
) -> VerificationResult:
    settings = get_settings()
    if is_locked(session):
        return VerificationResult(verified=False, locked=True)
    matches = await _find_matching(db, last_name, date_of_birth, normalise_phone(phone))
    if len(matches) == 1:
        _bind(session, matches[0])
        return VerificationResult(verified=True, patient=matches[0])
    session.failed_verifications += 1
    remaining = max(settings.max_verification_attempts - session.failed_verifications, 0)
    return VerificationResult(verified=False, attempts_remaining=remaining, locked=remaining == 0)


@dataclass
class RegistrationResult:
    patient: Patient
    already_registered: bool


async def register_patient(
    db: AsyncSession,
    session: VoiceSession,
    *,
    first_name: str,
    last_name: str,
    date_of_birth: date,
    phone: str,
    postcode: str | None = None,
    email: str | None = None,
) -> RegistrationResult:
    if is_locked(session):
        raise PatientInputError("Identity checks are locked for this conversation")
    validate_dob(date_of_birth)
    compact_phone = normalise_phone(phone)
    surname = clean_name(last_name)
    # Same three facts we use for verification: if they match an existing
    # record, this *is* that patient - don't create a duplicate.
    existing = await _find_matching(db, surname, date_of_birth, compact_phone)
    if len(existing) == 1:
        _bind(session, existing[0])
        return RegistrationResult(existing[0], already_registered=True)

    patient = Patient(
        first_name=clean_name(first_name),
        last_name=surname,
        date_of_birth=date_of_birth,
        postcode=normalise_postcode(postcode) if postcode else None,
        phone=compact_phone,
        email=(email or "").strip() or None,
    )
    db.add(patient)
    await db.flush()
    _bind(session, patient)
    return RegistrationResult(patient, already_registered=False)


def is_minor(patient: Patient) -> bool:
    return age_on(patient.date_of_birth, timeutil.clinic_today()) < MINOR_AGE
