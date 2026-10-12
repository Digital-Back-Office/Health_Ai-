import { CalendarDays, Check, LoaderCircle, LogOut, RefreshCw, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, api } from "../lib/api";
import { dayLabel, timeLabel } from "../lib/format";
import type { ClinicInfo, StaffAppointment } from "../lib/types";
import { Header } from "./Chrome";
import { StaffCalendar } from "./StaffCalendar";

const TOKEN_KEY = "hc-staff-token";

interface Callback {
  id: string;
  name: string | null;
  phone: string;
  reason: string;
  preferred_time: string | null;
  created_at: string;
  known_patient: boolean;
}

interface ConversationRow {
  conversation_id: string;
  status: string | null;
  call_successful: string | null;
  summary: string | null;
  duration_seconds: number | null;
  data_collection: Record<string, unknown>;
  received_at: string;
}

interface Stats {
  upcoming_appointments: number;
  appointments_today: number;
  booked_by_assistant_today: number;
  open_callbacks: number;
  conversations_today: number;
  calendar_sync_failures: number;
}

type Tab = "calendar" | "appointments" | "callbacks" | "conversations";

const SYNC_BADGE: Record<string, string> = {
  synced: "badge--success",
  pending: "badge--warning",
  failed: "badge--danger",
  not_required: "",
};

function readToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function isoDate(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toLocaleDateString("en-CA", { timeZone: "Europe/London" });
}

export function StaffDashboard({ clinic }: { clinic: ClinicInfo | null }) {
  const [token, setToken] = useState(readToken);
  const [draftToken, setDraftToken] = useState("");
  const [tab, setTab] = useState<Tab>("calendar");
  const [from, setFrom] = useState(isoDate(0));
  const [to, setTo] = useState(isoDate(13));
  const [stats, setStats] = useState<Stats | null>(null);
  const [appointments, setAppointments] = useState<StaffAppointment[]>([]);
  const [callbacks, setCallbacks] = useState<Callback[]>([]);
  const [conversations, setConversations] = useState<ConversationRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signOut = useCallback(() => {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* ignore */
    }
    setToken("");
  }, []);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const [s, a, c, v] = await Promise.all([
        api.staff<Stats>("/stats", token),
        api.staff<{ appointments: StaffAppointment[] }>(`/appointments?date_from=${from}&date_to=${to}`, token),
        api.staff<{ callbacks: Callback[] }>("/callbacks", token),
        api.staff<{ conversations: ConversationRow[] }>("/conversations", token),
      ]);
      setStats(s);
      setAppointments(a.appointments);
      setCallbacks(c.callbacks);
      setConversations(v.conversations);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        signOut();
        setError("That access token wasn't accepted.");
      } else {
        setError((err as Error).message);
      }
    } finally {
      setLoading(false);
    }
  }, [token, from, to, signOut]);

  useEffect(() => {
    void load();
    if (!token) return;
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load, token]);

  const signIn = (event: FormEvent) => {
    event.preventDefault();
    const value = draftToken.trim();
    if (!value) return;
    try {
      sessionStorage.setItem(TOKEN_KEY, value);
    } catch {
      /* ignore */
    }
    setToken(value);
    setDraftToken("");
  };

  const completeCallback = async (id: string) => {
    await api.staff(`/callbacks/${id}/done`, token, { method: "POST" });
    void load();
  };

  const resync = async () => {
    await api.staff("/calendar/resync", token, { method: "POST" });
    void load();
  };

  if (!token) {
    return (
      <>
        <Header clinic={clinic} right={<a className="pill" href="#/">Patient view</a>} />
        <main className="shell">
          <form className="card login" onSubmit={signIn}>
            <span className="eyebrow">Staff area</span>
            <h1 className="staff__title">Clinic dashboard</h1>
            <p className="muted">Bookings, callbacks and conversation summaries from the AI receptionist.</p>
            {error && <div className="notice notice--error">{error}</div>}
            <label className="field">
              Access token
              <input type="password" value={draftToken} onChange={(e) => setDraftToken(e.target.value)} autoFocus autoComplete="off" />
              <span className="subtle">This is the ADMIN_TOKEN value in backend/.env.</span>
            </label>
            <button className="btn btn--primary" type="submit">
              <ShieldCheck size={17} /> Sign in
            </button>
          </form>
        </main>
      </>
    );
  }

  const tiles: [string, number | undefined][] = [
    ["Appointments today", stats?.appointments_today],
    ["Upcoming", stats?.upcoming_appointments],
    ["Booked by AI today", stats?.booked_by_assistant_today],
    ["Open callbacks", stats?.open_callbacks],
    ["Conversations today", stats?.conversations_today],
    ["Calendar sync failures", stats?.calendar_sync_failures],
  ];

  return (
    <>
      <Header
        clinic={clinic}
        right={
          <>
            <a className="pill" href="#/">
              Patient view
            </a>
            <button className="pill" type="button" onClick={signOut} style={{ cursor: "pointer" }}>
              <LogOut size={14} /> Sign out
            </button>
          </>
        }
      />
      <main className="shell staff">
        <div className="staff__head">
          <div>
            <span className="eyebrow">Staff area</span>
            <h1 className="staff__title">Clinic dashboard</h1>
          </div>
          <div className="actions">
            <button className="btn btn--ghost btn--sm" type="button" onClick={resync}>
              <CalendarDays size={15} /> Resync Google Calendar
            </button>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => void load()} disabled={loading}>
              {loading ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />} Refresh
            </button>
          </div>
        </div>

        {error && <div className="notice notice--error">{error}</div>}

        <div className="tiles">
          {tiles.map(([label, value]) => (
            <div key={label} className="card tile">
              <span className="tile__value">{value ?? "–"}</span>
              <span className="tile__label">{label}</span>
            </div>
          ))}
        </div>

        <div className="tabs" role="tablist">
          {(
            [
              ["calendar", "Calendar"],
              ["appointments", "List"],
              ["callbacks", `Callbacks (${callbacks.length})`],
              ["conversations", "Conversations"],
            ] as const
          ).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
              {label}
            </button>
          ))}
        </div>

        {tab === "calendar" && <StaffCalendar token={token} clinic={clinic} onUnauthorized={signOut} />}

        {tab === "appointments" && (
          <section className="card">
            <div className="card__body filters">
              <label className="field">
                From
                <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
              </label>
              <label className="field">
                To
                <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
              </label>
            </div>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Patient</th>
                    <th>Service</th>
                    <th>Clinician</th>
                    <th>Ref</th>
                    <th>Status</th>
                    <th>Google Calendar</th>
                  </tr>
                </thead>
                <tbody>
                  {appointments.length === 0 && (
                    <tr>
                      <td colSpan={7} className="empty-row">
                        No appointments in this period.
                      </td>
                    </tr>
                  )}
                  {appointments.map((a) => (
                    <tr key={a.id} className={a.status === "cancelled" ? "is-cancelled" : undefined}>
                      <td>
                        <strong>{dayLabel(a.start_time)}</strong>
                        <br />
                        <span className="subtle">{timeLabel(a.start_time)}</span>
                      </td>
                      <td>
                        {a.patient.name}
                        <br />
                        <span className="subtle">
                          {a.patient.phone}
                          {a.patient.postcode ? ` · ${a.patient.postcode}` : ""}
                        </span>
                      </td>
                      <td>{a.service}</td>
                      <td>{a.clinician}</td>
                      <td className="mono">{a.reference}</td>
                      <td>
                        <span className={`badge ${a.status === "booked" ? "badge--success" : ""}`}>{a.status}</span>
                        {a.cancellation_reason && <div className="subtle">{a.cancellation_reason}</div>}
                      </td>
                      <td>
                        <span className={`badge ${SYNC_BADGE[a.calendar_sync_status] ?? ""}`} title={a.calendar_sync_error ?? undefined}>
                          {a.calendar_sync_status.replace("_", " ")}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {tab === "callbacks" && (
          <section className="card table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Requested</th>
                  <th>Name</th>
                  <th>Phone</th>
                  <th>Reason</th>
                  <th>Preferred time</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {callbacks.length === 0 && (
                  <tr>
                    <td colSpan={6} className="empty-row">
                      No open callback requests.
                    </td>
                  </tr>
                )}
                {callbacks.map((c) => (
                  <tr key={c.id}>
                    <td>
                      {dayLabel(c.created_at)}
                      <br />
                      <span className="subtle">{timeLabel(c.created_at)}</span>
                    </td>
                    <td>
                      {c.name ?? "Unknown caller"}
                      {c.known_patient && <div className="badge badge--brand">Verified patient</div>}
                    </td>
                    <td>
                      <a href={`tel:${c.phone}`}>{c.phone}</a>
                    </td>
                    <td>{c.reason}</td>
                    <td>{c.preferred_time ?? "Any time"}</td>
                    <td>
                      <button className="btn btn--ghost btn--sm" type="button" onClick={() => void completeCallback(c.id)}>
                        <Check size={15} /> Done
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}

        {tab === "conversations" && (
          <section className="card table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Received</th>
                  <th>Outcome</th>
                  <th>Summary</th>
                  <th>Length</th>
                </tr>
              </thead>
              <tbody>
                {conversations.length === 0 && (
                  <tr>
                    <td colSpan={4} className="empty-row">
                      No conversation summaries yet. They arrive via the ElevenLabs post-call webhook.
                    </td>
                  </tr>
                )}
                {conversations.map((c) => (
                  <tr key={c.conversation_id}>
                    <td>
                      {dayLabel(c.received_at)}
                      <br />
                      <span className="subtle">{timeLabel(c.received_at)}</span>
                    </td>
                    <td>
                      <span className={`badge ${c.call_successful === "success" ? "badge--success" : c.call_successful === "failure" ? "badge--danger" : ""}`}>
                        {String(c.data_collection.outcome ?? c.call_successful ?? "unknown")}
                      </span>
                    </td>
                    <td style={{ maxWidth: 520 }}>{c.summary ?? "–"}</td>
                    <td>{c.duration_seconds != null ? `${Math.round(c.duration_seconds / 60)} min` : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
      </main>
    </>
  );
}
