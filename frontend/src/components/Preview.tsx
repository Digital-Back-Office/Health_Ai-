/**
 * Development-only UI preview (#/preview): renders the visit panel states with
 * sample data, so the interface can be reviewed without using ElevenLabs minutes.
 * Not included in production builds.
 */
import { useReducer, useState } from "react";
import type { Appointment, ClinicInfo, SessionEvent, Slot } from "../lib/types";
import type { HumanCall, HumanCallState } from "../lib/useHumanCall";
import { initialVisit, visitReducer } from "../lib/visit";
import { Footer, Header } from "./Chrome";
import { ClinicCard } from "./ClinicCard";
import { HumanCallPanel, SosSheet } from "./HumanCall";
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

const DOCTORS = (patel: string, whitfield: string) => [
  { name: "Dr James Whitfield", status: whitfield },
  { name: "Dr Sarah Patel", status: patel },
];

// Calls with people (SOS doctors / reception), without Twilio.
const CALLS: Record<string, Partial<HumanCallState>> = {
  "sos starting": { kind: "sos", phase: "starting" },
  "sos ringing": { kind: "sos", phase: "ringing", doctors: DOCTORS("ringing", "ringing") },
  "sos connected": { kind: "sos", phase: "connected", doctors: DOCTORS("joined", "declined") },
  "sos unanswered": { kind: "sos", phase: "unanswered", doctors: DOCTORS("no-answer", "no-answer") },
  "sos error": {
    kind: "sos",
    phase: "error",
    error: "Our emergency line can't reach a doctor right now. If this is life-threatening, call 999 now.",
  },
  "sos ended": { kind: "sos", phase: "ended", doctors: DOCTORS("left", "declined") },
  "reception call": { kind: "reception", phase: "connected" },
};

function fakeCall(state: Partial<HumanCallState>, reset: () => void): HumanCall {
  const noop = async () => {};
  return {
    kind: null,
    phase: "idle",
    doctors: [],
    muted: false,
    since: state.phase === "starting" ? null : Date.now() - 42_000,
    error: null,
    ...state,
    startSos: noop,
    joinSos: noop,
    callReception: noop,
    hangUp: reset,
    reset,
    toggleMute: () => {},
  };
}

export default function Preview({ clinic }: { clinic: ClinicInfo | null }) {
  const [visit, dispatch] = useReducer(visitReducer, initialVisit);
  const [call, setCall] = useState<string | null>(null);
  const [sheet, setSheet] = useState<"line" | "no-line" | null>(null);
  return (
    <>
      <Header clinic={clinic} />
      <main className="shell page">
        <div className="stack">
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
              <p className="muted">Calls with people (SOS / reception):</p>
              <div className="suggestions" style={{ justifyContent: "flex-start" }}>
                <button className="chip" type="button" onClick={() => setSheet("line")}>
                  SOS sheet
                </button>
                <button className="chip" type="button" onClick={() => setSheet("no-line")}>
                  SOS sheet (line off)
                </button>
                {Object.keys(CALLS).map((name) => (
                  <button key={name} className="chip" type="button" aria-pressed={call === name} onClick={() => setCall(name)}>
                    {name}
                  </button>
                ))}
              </div>
            </div>
          </section>
          {call && (
            <section className="card assistant" style={{ minHeight: 0 }}>
              <HumanCallPanel call={fakeCall(CALLS[call], () => setCall(null))} assistant={clinic?.assistant_name ?? "Sophie"} />
            </section>
          )}
          {sheet && (
            <SosSheet
              assistant={clinic?.assistant_name ?? "Sophie"}
              emergencyLine={sheet === "line"}
              onClose={() => setSheet(null)}
              onConfirm={() => {
                setSheet(null);
                setCall("sos starting");
              }}
            />
          )}
        </div>
        <div className="stack">
          <VisitPanel visit={visit} live clinic={clinic} onPickSlot={() => {}} />
          <ClinicCard clinic={clinic} />
        </div>
      </main>
      <Footer />
    </>
  );
}
