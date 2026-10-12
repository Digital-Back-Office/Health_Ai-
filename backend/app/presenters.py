"""Turn domain objects into JSON for the agent (tool results) and the web UI.

Every date/time carries both a machine value (ISO 8601, clinic-local with
offset) and a "spoken" form, so the agent never has to format dates itself.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import timeutil
from .models import Appointment, Clinician, Patient, Service
from .services.availability import Slot


def spoken_reference(reference: str) -> str:
    return ", ".join(reference)


def slot_payload(slot: Slot, today: date | None = None) -> dict[str, Any]:
    local = timeutil.to_clinic(slot.starts_at)
    return {
        "clinician_id": slot.clinician_id,
        "clinician_name": slot.clinician_name,
        "start_time": timeutil.iso_local(slot.starts_at),
        "end_time": timeutil.iso_local(slot.ends_at),
        "date": local.date().isoformat(),
        "spoken": timeutil.speak_datetime(slot.starts_at, today),
    }


def service_payload(service: Service) -> dict[str, Any]:
    return {
        "code": service.code,
        "name": service.name,
        "duration_minutes": service.duration_minutes,
        "price": timeutil.format_price(service.price_pence),
        "price_pence": service.price_pence,
        "description": service.description,
        "preparation": service.preparation,
    }


def clinician_payload(clinician: Clinician) -> dict[str, Any]:
    return {
        "id": clinician.id,
        "name": clinician.full_name,
        "role": clinician.role,
        "bio": clinician.bio,
        "services": [s.code for s in clinician.services if s.is_active],
        "working_hours": [
            {"weekday": w.weekday, "start": f"{w.start_time:%H:%M}", "end": f"{w.end_time:%H:%M}"}
            for w in clinician.working_hours
        ],
    }


def appointment_payload(appointment: Appointment, today: date | None = None) -> dict[str, Any]:
    service = appointment.service
    return {
        "reference": appointment.reference,
        "reference_spoken": spoken_reference(appointment.reference),
        "status": appointment.status,
        "service_code": service.code,
        "service": service.name,
        "clinician_id": appointment.clinician_id,
        "clinician": appointment.clinician.full_name,
        "start_time": timeutil.iso_local(appointment.starts_at),
        "end_time": timeutil.iso_local(appointment.ends_at),
        "spoken": timeutil.speak_datetime(appointment.starts_at, today),
        "duration_minutes": service.duration_minutes,
        "price": timeutil.format_price(service.price_pence),
        "preparation": service.preparation,
    }


def patient_greeting_payload(patient: Patient) -> dict[str, Any]:
    """The only patient details we ever echo back: enough to greet them."""
    return {"first_name": patient.first_name}
