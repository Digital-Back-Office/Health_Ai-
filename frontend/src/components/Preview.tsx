/**
 * Development-only UI preview (#/preview): renders the visit panel states with
 * sample data, so the interface can be reviewed without using ElevenLabs minutes.
 * Not included in production builds.
 */
import { useReducer } from "react";
import type { Appointment, ClinicInfo, SessionEvent, Slot } from "../lib/types";
import { initialVisit, visitReducer } from "../lib/visit";
import { Footer, Header } from "./Chrome";
import { ClinicCard } from "./ClinicCard";
import { VisitPanel } from "./VisitPanel";

const pad = (n: number) => String(n).padStart(2, "0");

/** London-date (YYYY-MM-DD) for today + daysAhead. */
function londonDate(daysAhead: number): string {
  return new Date(Date.now() + daysAhead * 86_400_000).toLocaleDateString("en-CA", { timeZone: "Europe/London" });
}

/** ISO instant for a London wall-clock time, whatever the browser's own timezone is. */
function at(daysAhead: number, hour: number, minute = 0): string {
  const guess = new Date(`${londonDate(daysAhead)}T${pad(hour)}:${pad(minute)}:00Z`);
  const londonHour = Number(
    new Intl.DateTimeFormat("en-GB", { hour: "numeric", hourCycle: "h23", timeZone: "Europe/London" }).format(guess),
  );
  return new Date(guess.getTime() - (londonHour - guess.getUTCHours()) * 3_600_000).toISOString();
}

function slot(daysAhead: number, hour: number, minute: number, clinicianId: number, name: string): Slot {
  const start = at(daysAhead, hour, minute);
  return {
    clinician_id: clinicianId,
    clinician_name: name,
    start_time: start,
    end_time: new Date(new Date(start).getTime() + 20 * 60000).toISOString(),
    date: londonDate(daysAhead),
    spoken: "",
  };
}

const SERVICE = {
  code: "gp_consultation",
  name: "Private GP consultation",
  duration_minutes: 20,
  price: "£95",
  price_pence: 9500,
  description: "",
  preparation: "Please bring a list of any medicines you take and arrive five minutes early.",
};

const APPOINTMENT: Appointment = {
  reference: "7K3P9Q",
  reference_spoken: "7, K, 3, P, 9, Q",
  status: "booked",
  service_code: SERVICE.code,
  service: SERVICE.name,
  clinician_id: 1,
  clinician: "Dr Sarah Patel",
  start_time: at(2, 9, 30),
  end_time: at(2, 9, 50),
  spoken: "",
  duration_minutes: 20,
  price: "£95",
  preparation: SERVICE.preparation,
};

const SCRIPT: SessionEvent[] = [
  { type: "identity_verified", data: { first_name: "Alice", appointments: [] } },
  { type: "tool_started", data: { tool: "check_availability" } },
  {
    type: "slots_offered",
    data: {
      service: SERVICE,
      slots: [
        slot(1, 9, 0, 1, "Dr Sarah Patel"),
        slot(1, 9, 15, 2, "Dr James Whitfield"),
        slot(1, 14, 30, 1, "Dr Sarah Patel"),
        slot(2, 9, 30, 1, "Dr Sarah Patel"),
        slot(2, 11, 0, 2, "Dr James Whitfield"),
      ],
      next_available: [],
    },
  },
  { type: "appointment_booked", data: { appointment: APPOINTMENT } },
  {
    type: "appointment_rescheduled",
    data: {
      appointment: { ...APPOINTMENT, start_time: at(3, 15, 0), end_time: at(3, 15, 20) },
      previous: { spoken: "", start_time: APPOINTMENT.start_time, clinician: "Dr Sarah Patel" },
    },
  },
  { type: "appointment_cancelled", data: { appointment: { ...APPOINTMENT, status: "cancelled" }, late_cancellation: true } },
  { type: "callback_requested", data: { preferred_time: "Tomorrow morning" } },
];

export default function Preview({ clinic }: { clinic: ClinicInfo | null }) {
  const [visit, dispatch] = useReducer(visitReducer, initialVisit);
  return (
    <>
      <Header clinic={clinic} />
      <main className="shell page">
        <section className="card">
          <div className="card__header">
            <h1 className="card__title">UI preview (development only)</h1>
          </div>
          <div className="card__body" style={{ display: "grid", gap: 10 }}>
            <p className="muted">Replay the live events the backend sends during a conversation.</p>
            <div className="suggestions" style={{ justifyContent: "flex-start" }}>
              <button className="chip" type="button" onClick={() => dispatch({ type: "reset" })}>
                Reset
              </button>
              {SCRIPT.map((event, index) => (
                <button key={index} className="chip" type="button" onClick={() => dispatch(event)}>
                  {event.type.replace(/_/g, " ")}
                </button>
              ))}
            </div>
          </div>
        </section>
        <div className="stack">
          <VisitPanel visit={visit} live clinic={clinic} onPickSlot={() => {}} />
          <ClinicCard clinic={clinic} />
        </div>
      </main>
      <Footer />
    </>
  );
}
