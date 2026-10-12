"""Database models.

Postgres is the source of truth for scheduling. Google Calendar is a mirror
(plus a source of staff "busy" blocks), so every booking rule that matters for
correctness - above all "no double booking" - is enforced here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AppointmentStatus(enum.StrEnum):
    BOOKED = "booked"
    CANCELLED = "cancelled"


class CalendarSyncStatus(enum.StrEnum):
    NOT_REQUIRED = "not_required"  # integration disabled, or clinician has no calendar
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"


clinician_services = Table(
    "clinician_services",
    Base.metadata,
    Column("clinician_id", ForeignKey("clinicians.id", ondelete="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
)


class Service(Base):
    """A bookable appointment type, e.g. "Private GP consultation"."""

    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    preparation: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price_pence: Mapped[int] = mapped_column(Integer)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)

    # Not eager: Clinician.services is, and eager-loading both directions forms
    # a cycle SQLAlchemy cuts short. Load explicitly with selectinload() if needed.
    clinicians: Mapped[list[Clinician]] = relationship(
        secondary=clinician_services, back_populates="services", lazy="raise_on_sql"
    )

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="service_duration_positive"),
        CheckConstraint("price_pence >= 0", name="service_price_non_negative"),
    )


class Clinician(Base):
    __tablename__ = "clinicians"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(120))
    bio: Mapped[str] = mapped_column(Text, default="")
    google_calendar_id: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)

    services: Mapped[list[Service]] = relationship(
        secondary=clinician_services, back_populates="clinicians", lazy="selectin"
    )
    working_hours: Mapped[list[WorkingHours]] = relationship(
        back_populates="clinician",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="(WorkingHours.weekday, WorkingHours.start_time)",
    )


class WorkingHours(Base):
    """A recurring weekly working window in clinic-local time (0 = Monday)."""

    __tablename__ = "working_hours"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinician_id: Mapped[int] = mapped_column(ForeignKey("clinicians.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)

    clinician: Mapped[Clinician] = relationship(back_populates="working_hours")

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="working_hours_weekday_range"),
        CheckConstraint("end_time > start_time", name="working_hours_order"),
        Index("ix_working_hours_clinician_weekday", "clinician_id", "weekday"),
    )


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    date_of_birth: Mapped[date] = mapped_column(Date)
    # Optional; stored normalised: upper case, no spaces (e.g. "SW1A1AA").
    postcode: Mapped[str | None] = mapped_column(String(8))
    # Stored normalised (UK format, digits only, e.g. "07700900123"). Part of identity checks.
    phone: Mapped[str] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Identity checks look patients up by date of birth + phone, then compare surnames.
    __table_args__ = (Index("ix_patients_identity", "date_of_birth", "phone"),)


class VoiceSession(Base):
    """One conversation with the assistant (one browser tab, later one phone call).

    The raw session token is only ever held by the client; we store its hash.
    """

    __tablename__ = "voice_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    channel: Mapped[str] = mapped_column(String(16), default="web")
    mode: Mapped[str] = mapped_column(String(16), default="voice")
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_verifications: Mapped[int] = mapped_column(Integer, default=0)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    patient: Mapped[Patient | None] = relationship(lazy="selectin")

    __table_args__ = (Index("ix_voice_sessions_active", "ended_at", "last_seen_at"),)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(12), unique=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id", ondelete="RESTRICT"))
    clinician_id: Mapped[int] = mapped_column(ForeignKey("clinicians.id", ondelete="RESTRICT"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=AppointmentStatus.BOOKED)
    booked_via: Mapped[str] = mapped_column(String(32), default="ai_receptionist")
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("voice_sessions.id", ondelete="SET NULL"))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Google Calendar mirror. The event id is derived from the appointment id,
    # which makes calendar writes idempotent and safe to retry.
    calendar_sync_status: Mapped[str] = mapped_column(String(16), default=CalendarSyncStatus.PENDING)
    calendar_sync_error: Mapped[str | None] = mapped_column(Text)
    synced_calendar_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    patient: Mapped[Patient] = relationship(lazy="selectin")
    clinician: Mapped[Clinician] = relationship(lazy="selectin")
    service: Mapped[Service] = relationship(lazy="selectin")

    __table_args__ = (
        # The core double-booking guard: no two *booked* appointments for the
        # same clinician may overlap in time. Enforced atomically by Postgres,
        # so it holds no matter how many sessions book concurrently.
        ExcludeConstraint(
            ("clinician_id", "="),
            (text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            name="no_overlapping_appointments",
            using="gist",
            where=text("status = 'booked'"),
        ),
        CheckConstraint("ends_at > starts_at", name="appointment_time_order"),
        CheckConstraint("status IN ('booked', 'cancelled')", name="appointment_status_valid"),
        Index("ix_appointments_patient_starts", "patient_id", "starts_at"),
        Index("ix_appointments_clinician_starts", "clinician_id", "starts_at"),
        Index("ix_appointments_calendar_sync", "calendar_sync_status"),
    )


class AppointmentEvent(Base):
    """Append-only audit trail of everything that happens to an appointment."""

    __tablename__ = "appointment_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("voice_sessions.id", ondelete="SET NULL"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallbackRequest(Base):
    __tablename__ = "callback_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("voice_sessions.id", ondelete="SET NULL"))
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    preferred_time: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    patient: Mapped[Patient | None] = relationship(lazy="selectin")


class ConversationRecord(Base):
    """Post-call analysis delivered by the ElevenLabs post-call webhook."""

    __tablename__ = "conversation_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[str] = mapped_column(String(64), unique=True)
    agent_id: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("voice_sessions.id", ondelete="SET NULL"))
    status: Mapped[str | None] = mapped_column(String(32))
    call_successful: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    data_collection: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    transcript: Mapped[list[Any] | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
