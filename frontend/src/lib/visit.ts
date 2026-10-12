import type { Appointment, SessionEvent, SessionState, Service, Slot } from "./types";

export type Confirmation =
  | { kind: "booked"; appointment: Appointment }
  | { kind: "rescheduled"; appointment: Appointment; previous: { spoken: string; start_time: string; clinician: string } }
  | { kind: "cancelled"; appointment: Appointment; late: boolean };

export interface VisitState {
  verified: boolean;
  firstName: string | null;
  newPatient: boolean;
  locked: boolean;
  activity: string | null;
  offer: { service?: Service; slots: Slot[]; alternative: boolean } | null;
  confirmation: Confirmation | null;
  appointments: Appointment[];
  callbackRequested: boolean;
}

export const initialVisit: VisitState = {
  verified: false,
  firstName: null,
  newPatient: false,
  locked: false,
  activity: null,
  offer: null,
  confirmation: null,
  appointments: [],
  callbackRequested: false,
};

const byStart = (a: Appointment, b: Appointment) => a.start_time.localeCompare(b.start_time);

function upsert(list: Appointment[], appointment: Appointment): Appointment[] {
  const rest = list.filter((a) => a.reference !== appointment.reference);
  return appointment.status === "booked" ? [...rest, appointment].sort(byStart) : rest;
}

export type VisitAction = SessionEvent | { type: "reset" } | { type: "clear_activity" } | { type: "hydrate"; state: SessionState };

export function visitReducer(state: VisitState, action: VisitAction): VisitState {
  switch (action.type) {
    case "reset":
      return initialVisit;
    case "clear_activity":
      return { ...state, activity: null };
    case "hydrate":
      return {
        ...state,
        verified: action.state.verified,
        firstName: action.state.first_name,
        locked: action.state.locked,
        appointments: action.state.appointments,
      };
    case "tool_started":
      return { ...state, activity: action.data.tool };
    case "identity_verified":
      return {
        ...state,
        activity: null,
        verified: true,
        firstName: action.data.first_name,
        newPatient: Boolean(action.data.new_patient),
        appointments: [...action.data.appointments].sort(byStart),
      };
    case "identity_locked":
      return { ...state, activity: null, locked: true };
    case "slots_offered": {
      const slots = action.data.slots.length ? action.data.slots : action.data.next_available;
      return {
        ...state,
        activity: null,
        offer: {
          service: action.data.service ?? state.offer?.service,
          slots,
          alternative: Boolean(action.data.replaced) || (!action.data.slots.length && slots.length > 0),
        },
      };
    }
    case "appointment_booked":
      return {
        ...state,
        activity: null,
        offer: null,
        confirmation: { kind: "booked", appointment: action.data.appointment },
        appointments: upsert(state.appointments, action.data.appointment),
      };
    case "appointment_rescheduled":
      return {
        ...state,
        activity: null,
        offer: null,
        confirmation: { kind: "rescheduled", appointment: action.data.appointment, previous: action.data.previous },
        appointments: upsert(state.appointments, action.data.appointment),
      };
    case "appointment_cancelled":
      return {
        ...state,
        activity: null,
        confirmation: { kind: "cancelled", appointment: action.data.appointment, late: action.data.late_cancellation },
        appointments: upsert(state.appointments, action.data.appointment),
      };
    case "appointments_listed":
      return { ...state, activity: null, appointments: [...action.data.appointments].sort(byStart) };
    case "callback_requested":
      return { ...state, activity: null, callbackRequested: true };
    default:
      return state;
  }
}

export const TOOL_LABELS: Record<string, string> = {
  check_availability: "Checking the diary",
  book_appointment: "Booking your appointment",
  reschedule_appointment: "Moving your appointment",
  cancel_appointment: "Cancelling your appointment",
};
