"""Sample clinic data (idempotent). Replace with your clinic's real details.

Patients here are synthetic test identities - never seed real patient data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Clinician, Patient, Service, WorkingHours


@dataclass(frozen=True)
class ServiceSeed:
    code: str
    name: str
    duration_minutes: int
    price_pence: int
    description: str
    preparation: str


SERVICES: list[ServiceSeed] = [
    ServiceSeed(
        "gp_consultation",
        "Private GP consultation",
        20,
        9500,
        "A face-to-face appointment with one of our private GPs for any new or ongoing health concern.",
        "Please bring a list of any medicines you take and arrive five minutes early.",
    ),
    ServiceSeed(
        "gp_extended",
        "Extended GP consultation",
        40,
        16500,
        "A longer GP appointment for complex or several concerns, or a detailed review.",
        "Please bring a list of any medicines you take and any letters or results you'd like to discuss.",
    ),
    ServiceSeed(
        "health_check",
        "Well-person health check",
        60,
        29500,
        "A comprehensive check-up with a GP, including blood tests, blood pressure, BMI and a lifestyle review.",
        "Please don't eat for 8 hours beforehand (water is fine), so a morning appointment is best.",
    ),
    ServiceSeed(
        "physio_initial",
        "Physiotherapy initial assessment",
        45,
        9000,
        "Assessment and first treatment for back, neck, joint and sports injuries.",
        "Please wear loose, comfortable clothing and bring any scans or reports.",
    ),
    ServiceSeed(
        "physio_followup",
        "Physiotherapy follow-up",
        30,
        7000,
        "A follow-up treatment session after an initial physiotherapy assessment.",
        "Please wear loose, comfortable clothing.",
    ),
    ServiceSeed(
        "blood_test",
        "Blood test",
        15,
        4500,
        "A blood sample taken by our nurse. Laboratory fees depend on the tests requested and are charged separately.",
        "Drink plenty of water beforehand. If your doctor asked for a fasting test, don't eat for 10 to 12 hours before.",
    ),
    ServiceSeed(
        "travel_consultation",
        "Travel health consultation",
        20,
        4500,
        "Travel health advice and vaccinations before you go abroad. Vaccines are charged separately.",
        "Book at least 6 to 8 weeks before you travel, and bring your itinerary and vaccination record.",
    ),
    ServiceSeed(
        "dental_consultation",
        "Dental consultation",
        30,
        7500,
        "A check-up and consultation with our dentist: examination of teeth and gums, advice and a treatment plan. "
        "X-rays and treatment are charged separately.",
        "Please bring a list of any medicines you take and details of any previous dental treatment. "
        "Brush your teeth beforehand if you can.",
    ),
]

WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


@dataclass(frozen=True)
class ClinicianSeed:
    full_name: str
    role: str
    bio: str
    services: tuple[str, ...]
    # (days, start, end) in clinic-local time
    hours: tuple[tuple[tuple[str, ...], str, str], ...]


WEEKDAYS_MON_FRI = ("Mon", "Tue", "Wed", "Thu", "Fri")

CLINICIANS: list[ClinicianSeed] = [
    ClinicianSeed(
        "Dr Sarah Patel",
        "Private GP",
        "GMC-registered GP with a special interest in women's health.",
        ("gp_consultation", "gp_extended", "health_check"),
        ((WEEKDAYS_MON_FRI, "08:30", "13:00"), (WEEKDAYS_MON_FRI, "14:00", "18:30")),
    ),
    ClinicianSeed(
        "Dr James Whitfield",
        "Private GP",
        "GMC-registered GP with an interest in men's health and travel medicine.",
        ("gp_consultation", "gp_extended", "travel_consultation"),
        ((("Mon", "Tue", "Thu"), "09:00", "13:00"), (("Mon", "Tue", "Thu"), "14:00", "19:00"), (("Sat",), "09:00", "13:00")),
    ),
    ClinicianSeed(
        "Emma Clarke",
        "Chartered Physiotherapist",
        "HCPC-registered physiotherapist specialising in sports and musculoskeletal injuries.",
        ("physio_initial", "physio_followup"),
        ((("Tue", "Wed", "Thu", "Fri"), "08:00", "12:30"), (("Tue", "Wed", "Thu", "Fri"), "13:30", "19:00"), (("Sat",), "09:00", "13:00")),
    ),
    ClinicianSeed(
        "Olivia Reid",
        "Practice Nurse",
        "NMC-registered nurse for blood tests and travel vaccinations.",
        ("blood_test", "travel_consultation"),
        ((WEEKDAYS_MON_FRI, "08:00", "12:00"),),
    ),
    ClinicianSeed(
        "Dr Priya Shah",
        "Dentist",
        "GDC-registered dentist offering check-ups, consultations and treatment planning.",
        ("dental_consultation",),
        ((("Mon", "Wed", "Fri"), "09:00", "13:00"), (("Mon", "Wed", "Fri"), "14:00", "17:30")),
    ),
]

# Synthetic test patients. Phone numbers are from Ofcom's range reserved for drama.
DEMO_PATIENTS = [
    ("Alice", "Testington", date(1990, 4, 12), "EC1A1BB", "07700900123", "alice.testington@example.com"),
    ("Ben", "Sample", date(1985, 11, 3), "M11AE", "07700900456", None),
    ("Chloe", "Example", date(2016, 6, 1), "CR26XH", "07700900789", None),
]


def _t(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


async def seed(db: AsyncSession, *, demo_patients: bool = True) -> dict[str, int]:
    services: dict[str, Service] = {}
    for order, item in enumerate(SERVICES):
        service = (await db.execute(select(Service).where(Service.code == item.code))).scalar_one_or_none()
        if service is None:
            service = Service(code=item.code)
            db.add(service)
        service.name = item.name
        service.duration_minutes = item.duration_minutes
        service.price_pence = item.price_pence
        service.description = item.description
        service.preparation = item.preparation
        service.display_order = order
        service.is_active = True
        services[item.code] = service
    await db.flush()

    for item in CLINICIANS:
        clinician = (await db.execute(select(Clinician).where(Clinician.full_name == item.full_name))).scalar_one_or_none()
        if clinician is None:
            clinician = Clinician(full_name=item.full_name, working_hours=[], services=[])
            db.add(clinician)
        clinician.role = item.role
        clinician.bio = item.bio
        clinician.is_active = True
        clinician.services = [services[code] for code in item.services]
        clinician.working_hours = [
            WorkingHours(weekday=WEEKDAYS[day], start_time=_t(start), end_time=_t(end))
            for days, start, end in item.hours
            for day in days
        ]
    await db.flush()

    created_patients = 0
    if demo_patients:
        for first, last, dob, postcode, phone, email in DEMO_PATIENTS:
            exists = (
                await db.execute(
                    select(Patient.id).where(Patient.last_name == last, Patient.date_of_birth == dob, Patient.phone == phone)
                )
            ).first()
            if exists is None:
                db.add(Patient(first_name=first, last_name=last, date_of_birth=dob, postcode=postcode, phone=phone, email=email))
                created_patients += 1
    await db.commit()
    return {"services": len(SERVICES), "clinicians": len(CLINICIANS), "patients_created": created_patients}
