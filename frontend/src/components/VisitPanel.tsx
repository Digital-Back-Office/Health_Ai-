import {
  CalendarCheck,
  CalendarClock,
  CalendarDays,
  CalendarX,
  CircleCheck,
  Download,
  LoaderCircle,
  Lock,
  MessageSquareText,
  Navigation,
  PhoneCall,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { useMemo, useState } from "react";
import { dayLabel, downloadIcs, longDate, mapsLink, timeLabel } from "../lib/format";
import type { Appointment, ClinicInfo, Slot } from "../lib/types";
import { TOOL_LABELS, type Confirmation, type VisitState } from "../lib/visit";

interface Props {
  visit: VisitState;
  live: boolean;
  clinic: ClinicInfo | null;
  onPickSlot: (slot: Slot) => void;
}

function groupByDay(slots: Slot[]): [string, Slot[]][] {
  const groups = new Map<string, Slot[]>();
  for (const slot of slots) {
    const list = groups.get(slot.date) ?? [];
    list.push(slot);
    groups.set(slot.date, list);
  }
  return [...groups.entries()];
}

function ConfirmationCard({ confirmation, clinic }: { confirmation: Confirmation; clinic: ClinicInfo | null }) {
  const { appointment } = confirmation;
  const cancelled = confirmation.kind === "cancelled";
  const heading = {
    booked: "You're booked in",
    rescheduled: "Appointment moved",
    cancelled: "Appointment cancelled",
  }[confirmation.kind];
  const Icon = cancelled ? CalendarX : confirmation.kind === "rescheduled" ? CalendarClock : CircleCheck;

  return (
    <div className={`confirm${cancelled ? " confirm--cancelled" : ""}`} role="status">
      <div className="confirm__head">
        <Icon size={20} /> {heading}
      </div>
      <div className="confirm__body">
        <div>
          <div className="confirm__when">
            {longDate(appointment.start_time)} · {timeLabel(appointment.start_time)}
          </div>
          <div className="muted">
            {appointment.service} with {appointment.clinician}
          </div>
        </div>
        <div className="ref">
          Booking reference <code>{appointment.reference}</code>
        </div>
        <dl className="facts">
          <dt>Duration</dt>
          <dd>{appointment.duration_minutes} minutes</dd>
          <dt>Fee</dt>
          <dd>{appointment.price}</dd>
          {confirmation.kind === "rescheduled" && (
            <>
              <dt>Was</dt>
              <dd>
                {longDate(confirmation.previous.start_time)} · {timeLabel(confirmation.previous.start_time)}
              </dd>
            </>
          )}
          {clinic && !cancelled && (
            <>
              <dt>Where</dt>
              <dd>{clinic.address}</dd>
            </>
          )}
        </dl>
        {confirmation.kind === "cancelled" && confirmation.late && (
          <p className="prep">This was within 24 hours of the appointment, so our late cancellation policy may apply.</p>
        )}
        {!cancelled && appointment.preparation && <p className="prep">{appointment.preparation}</p>}
        {!cancelled && clinic && (
          <div className="actions">
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => downloadIcs(appointment, clinic.name, clinic.address)}>
              <Download size={15} /> Add to calendar
            </button>
            <a className="btn btn--ghost btn--sm" href={mapsLink(clinic.address)} target="_blank" rel="noreferrer">
              <Navigation size={15} /> Directions
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

function AppointmentRow({ appointment }: { appointment: Appointment }) {
  const date = new Date(appointment.start_time);
  const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", timeZone: "Europe/London" }).format(date);
  const month = new Intl.DateTimeFormat("en-GB", { month: "short", timeZone: "Europe/London" }).format(date);
  return (
    <li className="appt">
      <div className="appt__date">
        <b>{day}</b>
        <small>{month}</small>
      </div>
      <div className="appt__main">
        <span className="appt__title">{appointment.service}</span>
        <span className="subtle">
          {dayLabel(appointment.start_time)}, {timeLabel(appointment.start_time)} · {appointment.clinician} · Ref{" "}
          <span className="mono">{appointment.reference}</span>
        </span>
      </div>
    </li>
  );
}

export function VisitPanel({ visit, live, clinic, onPickSlot }: Props) {
  const [picked, setPicked] = useState<string | null>(null);
  const groups = useMemo(() => groupByDay(visit.offer?.slots ?? []), [visit.offer]);
  const nothingYet =
    !visit.verified && !visit.locked && !visit.offer && !visit.confirmation && !visit.appointments.length && !visit.activity && !visit.callbackRequested;

  return (
    <section className="card" aria-labelledby="visit-title">
      <div className="card__header">
        <h2 id="visit-title" className="card__title">
          <CalendarDays size={18} /> Your visit
        </h2>
        {live && <span className="badge badge--brand">Live</span>}
      </div>
      <div className="card__body" style={{ display: "grid", gap: 16 }}>
        {nothingYet && (
          <div className="visit-empty">
            <p className="muted">Everything we arrange together appears here as we go.</p>
            <ol className="steps">
              <li>
                <span className="steps__icon">
                  <MessageSquareText size={17} />
                </span>
                Tell us what you need
              </li>
              <li>
                <span className="steps__icon">
                  <UserRound size={17} />
                </span>
                Confirm your surname, date of birth and phone number
              </li>
              <li>
                <span className="steps__icon">
                  <CalendarClock size={17} />
                </span>
                Choose a time that suits you
              </li>
              <li>
                <span className="steps__icon">
                  <CalendarCheck size={17} />
                </span>
                Get your booking reference
              </li>
            </ol>
          </div>
        )}

        {visit.activity && (
          <div className="activity" role="status">
            <LoaderCircle size={17} className="spin" />
            {TOOL_LABELS[visit.activity] ?? "Working on it"}…
          </div>
        )}

        {visit.verified && (
          <div className="identity">
            <ShieldCheck size={18} />
            {visit.newPatient ? `Welcome to the clinic, ${visit.firstName}` : `Identity confirmed. Welcome back, ${visit.firstName}`}
          </div>
        )}
        {visit.locked && !visit.verified && (
          <div className="identity identity--locked">
            <Lock size={18} /> We couldn't confirm your identity. Please call us{clinic ? ` on ${clinic.phone}` : ""}.
          </div>
        )}

        {visit.confirmation && <ConfirmationCard confirmation={visit.confirmation} clinic={clinic} />}

        {visit.offer && visit.offer.slots.length > 0 && (
          <div>
            <div className="section-title">
              <span>
                {visit.offer.alternative ? "Nearest available times" : "Available times"}
                {visit.offer.service ? ` · ${visit.offer.service.name}` : ""}
              </span>
              {visit.offer.service && <span className="badge">{visit.offer.service.price}</span>}
            </div>
            {groups.map(([date, slots]) => (
              <div key={date} className="slot-day">
                <div className="slot-day__label">{dayLabel(slots[0].start_time)}</div>
                <div className="slot-grid">
                  {slots.map((slot) => {
                    const key = `${slot.clinician_id}-${slot.start_time}`;
                    return (
                      <button
                        key={key}
                        type="button"
                        className="slot"
                        aria-pressed={picked === key}
                        disabled={!live}
                        onClick={() => {
                          setPicked(key);
                          onPickSlot(slot);
                        }}
                      >
                        <span className="slot__time">{timeLabel(slot.start_time)}</span>
                        <span className="slot__who">{slot.clinician_name}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
            <p className="subtle" style={{ marginTop: 10 }}>
              Tap a time to choose it, or just say which one you'd like.
            </p>
          </div>
        )}
        {visit.offer && visit.offer.slots.length === 0 && (
          <div className="notice notice--warning">
            <CalendarX size={18} /> No times available for that search. You can ask for a different day, or for a callback.
          </div>
        )}

        {visit.appointments.length > 0 && (
          <div>
            <div className="section-title">Upcoming appointments</div>
            <ul className="appt-list">
              {visit.appointments.map((appointment) => (
                <AppointmentRow key={appointment.reference} appointment={appointment} />
              ))}
            </ul>
          </div>
        )}

        {visit.callbackRequested && (
          <div className="identity">
            <PhoneCall size={18} /> Callback requested. Our team will call you back, usually within one working day.
          </div>
        )}
      </div>
    </section>
  );
}
