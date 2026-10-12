import { LoaderCircle, PhoneCall, PhoneOutgoing, Siren, UsersRound } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, api } from "../lib/api";
import { dayLabel, timeLabel } from "../lib/format";
import type { SosPayload, StaffAppointment } from "../lib/types";

interface TelephonyStatus {
  configured: boolean;
  clinic_number: string | null;
  number_connected_to_agent: boolean;
  browser_calling: boolean;
  reception_number: string | null;
  sms_enabled: boolean;
  reminder_calls_enabled: boolean;
  reminder_call_hour: number;
  sos_doctors_available: number;
}

interface OnCallClinician {
  id: number;
  name: string;
  role: string;
  mobile_phone: string | null;
  sos_available: boolean;
}

interface EmergencyRow extends SosPayload {
  created_at: string;
  connected_at: string | null;
  ended_at: string | null;
  reason: string;
  patient: string | null;
  caller_number: string | null;
}

const CALL_STATUS: Record<string, string> = {
  queued: "calling",
  ringing: "ringing",
  answered: "answered",
  accepted: "joining",
  joined: "on the call",
  left: "joined, then left",
  declined: "declined",
  "no-answer": "no answer",
  busy: "busy",
  failed: "call failed",
  canceled: "stopped",
  completed: "no answer",
};

/** +447700900123 -> 07700 900123 (UK numbers read more easily the local way). */
export function ukNumber(value: string | null | undefined): string {
  if (!value) return "";
  const local = value.startsWith("+44") ? `0${value.slice(3)}` : value;
  return /^07\d{9}$/.test(local) ? `${local.slice(0, 5)} ${local.slice(5)}` : local;
}

function outcome(e: EmergencyRow): [string, string] {
  if (e.status === "dialling") return ["Ringing doctors now", "badge--warning"];
  if (e.status === "connected") return ["On a call now", "badge--success"];
  if (e.connected_at) return ["Doctor joined", "badge--success"];
  if (e.status === "unanswered") return ["No doctor answered", "badge--danger"];
  if (e.status === "failed") return ["Couldn't ring doctors", "badge--danger"];
  return ["Ended before a doctor joined", ""];
}

const REMINDER: Record<string, [string, string]> = {
  calling: ["Calling…", "badge--warning"],
  completed: ["Reminded", "badge--success"],
  failed: ["Not reached", "badge--danger"],
  skipped: ["Skipped", ""],
};

/** Reminder-call status for an appointment, with a button to ring the patient now. */
export function ReminderCell({ appointment: a, token, onChanged }: { appointment: StaffAppointment; token: string; onChanged?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const upcoming = a.status === "booked" && new Date(a.start_time).getTime() > Date.now();
  const [label, tone] = a.reminder_status ? (REMINDER[a.reminder_status] ?? [a.reminder_status, ""]) : ["Not called", ""];

  const call = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await api.staff(`/appointments/${a.id}/reminder-call`, token, { method: "POST" });
      setMessage("Calling the patient now…");
      onChanged?.();
    } catch (err) {
      setMessage((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="reminder">
      <span className={`badge ${tone}`}>{label}</span>
      {upcoming && (
        <button
          className="btn btn--ghost btn--sm"
          type="button"
          onClick={() => void call()}
          disabled={busy || a.reminder_status === "calling"}
          title="Sophie rings the patient about this appointment now"
        >
          {busy ? <LoaderCircle size={14} className="spin" /> : <PhoneOutgoing size={14} />} Call now
        </button>
      )}
      {message && <span className="subtle">{message}</span>}
    </div>
  );
}

function MobileForm({ clinician, saving, onSave }: { clinician: OnCallClinician; saving: boolean; onSave: (value: string) => void }) {
  const saved = ukNumber(clinician.mobile_phone);
  const [draft, setDraft] = useState(saved);
  useEffect(() => setDraft(saved), [saved]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSave(draft.trim());
  };
  return (
    <form className="inline-form" onSubmit={submit}>
      <label className="sr-only" htmlFor={`mobile-${clinician.id}`}>
        Mobile number for {clinician.name}
      </label>
      <input
        id={`mobile-${clinician.id}`}
        type="tel"
        inputMode="tel"
        placeholder="07700 900123"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        autoComplete="off"
        maxLength={24}
      />
      <button className="btn btn--ghost btn--sm" type="submit" disabled={saving || draft.trim() === saved}>
        {saving ? <LoaderCircle size={14} className="spin" /> : null} Save
      </button>
    </form>
  );
}

/** Staff tab: who is on call for SOS, the phone line's set-up, and recent emergencies. */
export function StaffOnCall({ token, onUnauthorized }: { token: string; onUnauthorized: () => void }) {
  const [status, setStatus] = useState<TelephonyStatus | null>(null);
  const [clinicians, setClinicians] = useState<OnCallClinician[]>([]);
  const [emergencies, setEmergencies] = useState<EmergencyRow[]>([]);
  const [saving, setSaving] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handle = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.status === 401) onUnauthorized();
      else setError((err as Error).message);
    },
    [onUnauthorized],
  );

  const load = useCallback(async () => {
    try {
      const [s, c, e] = await Promise.all([
        api.staff<TelephonyStatus>("/telephony", token),
        api.staff<{ clinicians: OnCallClinician[] }>("/clinicians", token),
        api.staff<{ emergencies: EmergencyRow[] }>("/emergencies", token),
      ]);
      setStatus(s);
      setClinicians(c.clinicians);
      setEmergencies(e.emergencies);
    } catch (err) {
      handle(err);
    }
  }, [token, handle]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const update = async (clinician: OnCallClinician, patch: { mobile_phone?: string | null; sos_available?: boolean }) => {
    setSaving(clinician.id);
    setError(null);
    try {
      const updated = await api.staff<OnCallClinician>(`/clinicians/${clinician.id}`, token, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      setClinicians((list) => list.map((c) => (c.id === updated.id ? updated : c)));
      void load();
    } catch (err) {
      handle(err);
    } finally {
      setSaving(null);
    }
  };

  const live = emergencies.filter((e) => e.status === "dialling" || e.status === "connected");
  const onCall = clinicians.filter((c) => c.sos_available).length;

  return (
    <div className="oncall">
      {live.length > 0 && (
        <div className="notice notice--error" role="alert">
          <Siren size={18} />
          <span>
            <strong>Emergency call in progress.</strong>{" "}
            {live.map((e) => e.patient ?? (ukNumber(e.caller_number) || "A website visitor")).join(", ")}: doctors are being
            connected now.
          </span>
        </div>
      )}
      {error && (
        <div className="notice notice--error" role="alert">
          <span>{error}</span>
        </div>
      )}

      <div className="oncall__grid">
        <section className="card">
          <div className="card__header">
            <h2 className="card__title">
              <UsersRound size={18} /> On call for SOS
            </h2>
            <span className={`badge ${onCall ? "badge--success" : "badge--warning"}`}>{onCall} on call</span>
          </div>
          <div className="card__body">
            <p className="muted">
              When a patient presses SOS or tells Sophie it's an emergency, everyone switched on here is rung at the same time.
              They hear who needs help and press 1 to join the call with the patient. Sophie is not on the call.
            </p>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Clinician</th>
                  <th>Mobile for emergency calls</th>
                  <th>On call</th>
                </tr>
              </thead>
              <tbody>
                {clinicians.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <strong>{c.name}</strong>
                      <br />
                      <span className="subtle">{c.role}</span>
                    </td>
                    <td>
                      <MobileForm
                        clinician={c}
                        saving={saving === c.id}
                        onSave={(value) => void update(c, { mobile_phone: value || null })}
                      />
                    </td>
                    <td>
                      <label className="switch" title={c.mobile_phone ? undefined : "Add a mobile number first"}>
                        <input
                          type="checkbox"
                          checked={c.sos_available}
                          disabled={!c.mobile_phone || saving === c.id}
                          onChange={(e) => void update(c, { sos_available: e.target.checked })}
                        />
                        <span className="switch__track" aria-hidden="true" />
                        <span>{c.sos_available ? "On call" : "Off"}</span>
                      </label>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="card">
          <div className="card__header">
            <h2 className="card__title">
              <PhoneCall size={18} /> Phone line
            </h2>
            {status && (
              <span className={`badge ${status.configured ? "badge--success" : "badge--warning"}`}>
                {status.configured ? "Connected" : "Not set up"}
              </span>
            )}
          </div>
          <div className="card__body">
            {!status ? (
              <p className="subtle">Loading…</p>
            ) : (
              <dl className="facts">
                <dt>Clinic number</dt>
                <dd>
                  {ukNumber(status.clinic_number) || "–"}
                  {status.configured && (
                    <div className="subtle">{status.number_connected_to_agent ? "Answered by Sophie" : "Run setup-agent to connect it to Sophie"}</div>
                  )}
                </dd>
                <dt>Calls from the website</dt>
                <dd>{status.browser_calling ? "Ready (SOS and reception)" : "Needs an API key and setup-agent"}</dd>
                <dt>Reception</dt>
                <dd>{ukNumber(status.reception_number) || "Not set (RECEPTION_PHONE_NUMBER)"}</dd>
                <dt>Text confirmations</dt>
                <dd>{status.sms_enabled ? "On" : "Off"}</dd>
                <dt>Reminder calls</dt>
                <dd>
                  {status.reminder_calls_enabled
                    ? `On: the evening before, from ${String(status.reminder_call_hour).padStart(2, "0")}:00`
                    : "Off (REMINDER_CALLS_ENABLED)"}
                </dd>
                <dt>SOS button</dt>
                <dd>
                  {status.configured && status.sos_doctors_available > 0
                    ? `Rings ${status.sos_doctors_available} clinician${status.sos_doctors_available === 1 ? "" : "s"}`
                    : "Only offers 999 (nobody on call)"}
                </dd>
              </dl>
            )}
            {status && !status.configured && (
              <p className="subtle" style={{ marginTop: 12 }}>
                Add the TWILIO_* settings to backend/.env and restart the backend. See “Phone calls &amp; SOS” in the README.
              </p>
            )}
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card__header">
          <h2 className="card__title">
            <Siren size={18} /> Recent emergency calls
          </h2>
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>When</th>
                <th>Patient</th>
                <th>From</th>
                <th>Reason</th>
                <th>Doctors rung</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {emergencies.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty-row">
                    No emergency calls yet.
                  </td>
                </tr>
              )}
              {emergencies.map((e) => {
                const [label, tone] = outcome(e);
                return (
                  <tr key={e.incident_id}>
                    <td>
                      <strong>{dayLabel(e.created_at)}</strong>
                      <br />
                      <span className="subtle">{timeLabel(e.created_at)}</span>
                    </td>
                    <td>
                      {e.patient ?? "Not verified"}
                      {e.caller_number && <div className="subtle">{ukNumber(e.caller_number)}</div>}
                    </td>
                    <td>{e.channel === "phone" ? "Phone call" : "Website"}</td>
                    <td>{e.reason || "–"}</td>
                    <td>
                      {e.doctors.map((d, index) => (
                        <div key={`${d.name}-${index}`}>
                          {d.name} <span className="subtle">· {CALL_STATUS[d.status] ?? d.status}</span>
                        </div>
                      ))}
                    </td>
                    <td>
                      <span className={`badge ${tone}`}>{label}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
