export interface Service {
  code: string;
  name: string;
  duration_minutes: number;
  price: string;
  price_pence: number;
  description: string;
  preparation: string;
}

export interface WorkingWindow {
  weekday: number; // 0 = Monday
  start: string; // "08:30", clinic-local
  end: string;
}

export interface Clinician {
  id: number;
  name: string;
  role: string;
  bio: string;
  services: string[];
  working_hours: WorkingWindow[];
}

export interface OpeningDay {
  weekday: number;
  day: string;
  open: string | null;
  close: string | null;
}

export interface ClinicInfo {
  name: string;
  assistant_name: string;
  address: string;
  phone: string;
  email: string;
  timezone: string;
  open_now: boolean;
  today: OpeningDay;
  opening_hours: OpeningDay[];
  /** The SOS button can ring our doctors (otherwise it only offers 999). */
  emergency_line: boolean;
  services: Service[];
  clinicians: Clinician[];
}

export interface Slot {
  clinician_id: number;
  clinician_name: string;
  start_time: string;
  end_time: string;
  date: string;
  spoken: string;
}

export interface Appointment {
  reference: string;
  reference_spoken: string;
  status: "booked" | "cancelled";
  service_code: string;
  service: string;
  clinician_id: number;
  clinician: string;
  start_time: string;
  end_time: string;
  spoken: string;
  duration_minutes: number;
  price: string;
  preparation: string;
}

/** Appointment as returned by the staff API (includes patient contact details). */
export interface StaffAppointment extends Appointment {
  id: string;
  patient: { name: string; date_of_birth: string; postcode: string | null; phone: string };
  calendar_sync_status: string;
  calendar_sync_error: string | null;
  booked_via: string;
  created_at: string;
  cancellation_reason: string | null;
  reminder_status: string | null;
}

export interface StartedSession {
  session_id: string;
  session_token: string;
  expires_at: string;
  mode: "voice" | "text";
  connection_type: "webrtc" | "websocket";
  conversation_token: string | null;
  signed_url: string | null;
  dynamic_variables: Record<string, string>;
}

export interface SessionState {
  verified: boolean;
  first_name: string | null;
  appointments: Appointment[];
  locked: boolean;
}

export type SessionEvent =
  | { type: "ready"; data: Record<string, never> }
  | { type: "tool_started"; data: { tool: string } }
  | { type: "identity_verified"; data: { first_name: string; appointments: Appointment[]; new_patient?: boolean } }
  | { type: "identity_locked"; data: Record<string, never> }
  | { type: "slots_offered"; data: { service?: Service; slots: Slot[]; next_available: Slot[]; replaced?: boolean } }
  | { type: "appointment_booked"; data: { appointment: Appointment } }
  | { type: "appointment_rescheduled"; data: { appointment: Appointment; previous: { spoken: string; start_time: string; clinician: string } } }
  | { type: "appointment_cancelled"; data: { appointment: Appointment; late_cancellation: boolean } }
  | { type: "appointments_listed"; data: { appointments: Appointment[] } }
  | { type: "callback_requested"; data: { preferred_time: string | null } }
  | { type: "state_changed"; data: Record<string, never> }
  | { type: "sos_started" | "sos_update" | "sos_connected" | "sos_unanswered" | "sos_ended"; data: SosPayload }
  | { type: "reception_requested"; data: { reason: string } };

export interface DoctorCallStatus {
  name: string;
  status: string;
}

export interface SosPayload {
  incident_id: string;
  status: "dialling" | "connected" | "unanswered" | "ended" | "failed";
  channel: "web" | "phone";
  doctors: DoctorCallStatus[];
}

export interface SosStarted {
  incident_id: string;
  doctors_called: number;
  session_id?: string;
  session_token?: string;
  expires_at?: string;
}

export interface TranscriptEntry {
  id: string;
  role: "user" | "agent";
  text: string;
  at: number;
}
