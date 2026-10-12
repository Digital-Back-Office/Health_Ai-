import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { ApiError, api } from "../lib/api";
import { dayLabel, longDate, timeLabel } from "../lib/format";
import type { ClinicInfo, StaffAppointment } from "../lib/types";

const HOUR_PX = 56;
const COLORS = ["#31a991", "#3b82c4", "#8b6fd6", "#d9963a", "#d0627a", "#5f7a8a"];

type View = "week" | "day";
type Range = [number, number]; // minutes since midnight, clinic time

// Dates are plain "YYYY-MM-DD" strings in clinic (London) time.
function londonToday(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" });
}

function addDays(ymd: string, days: number): string {
  const date = new Date(`${ymd}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

/** 0 = Monday ... 6 = Sunday */
function weekdayOf(ymd: string): number {
  return (new Date(`${ymd}T00:00:00Z`).getUTCDay() + 6) % 7;
}

function formatDate(ymd: string, options: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" }).format(new Date(`${ymd}T00:00:00Z`));
}

// Times from the API are clinic-local ISO strings with an offset, e.g. "2026-10-14T09:30+01:00",
// so the wall-clock date and time can be read straight off the string.
const datePart = (iso: string) => iso.slice(0, 10);
const minutesOf = (iso: string) => Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16));
const hhmmToMinutes = (value: string) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));

function clock(minutes: number): string {
  const hour = Math.floor(minutes / 60);
  const minute = minutes % 60;
  const suffix = hour < 12 ? "am" : "pm";
  const hour12 = hour % 12 || 12;
  return minute ? `${hour12}:${String(minute).padStart(2, "0")}${suffix}` : `${hour12}${suffix}`;
}

function londonNowMinutes(): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: "Europe/London",
  }).formatToParts(new Date());
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  return get("hour") * 60 + get("minute");
}

function weekTitle(first: string, last: string): string {
  const sameMonth = first.slice(0, 7) === last.slice(0, 7);
  const sameYear = first.slice(0, 4) === last.slice(0, 4);
  const start = formatDate(first, sameMonth ? { day: "numeric" } : sameYear ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" });
  const end = formatDate(last, sameMonth ? { day: "numeric", month: "long", year: "numeric" } : { day: "numeric", month: "short", year: "numeric" });
  return `${start} – ${end}`;
}

/** Parts of [start, end] not covered by the given working windows. */
function offHours(windows: Range[], start: number, end: number): Range[] {
  const gaps: Range[] = [];
  let cursor = start;
  for (const [from, to] of [...windows].sort((a, b) => a[0] - b[0])) {
    if (from > cursor) gaps.push([cursor, Math.min(from, end)]);
    cursor = Math.max(cursor, to);
  }
  if (cursor < end) gaps.push([cursor, end]);
  return gaps.filter(([from, to]) => to > from);
}

interface Placed {
  appointment: StaffAppointment;
  start: number;
  end: number;
  lane: number;
  lanes: number;
}

/** Puts overlapping appointments side by side, like any calendar app. */
function layoutLanes(appointments: StaffAppointment[]): Placed[] {
  const items: Placed[] = appointments
    .map((appointment) => {
      const start = minutesOf(appointment.start_time);
      return { appointment, start, end: Math.max(minutesOf(appointment.end_time), start + 10), lane: 0, lanes: 1 };
    })
    .sort((a, b) => a.start - b.start || a.end - b.end);
  let cluster: Placed[] = [];
  let clusterEnd = -1;
  let laneEnds: number[] = [];
  const closeCluster = () => {
    for (const item of cluster) item.lanes = Math.max(1, laneEnds.length);
    cluster = [];
    laneEnds = [];
    clusterEnd = -1;
  };
  for (const item of items) {
    if (cluster.length && item.start >= clusterEnd) closeCluster();
    let lane = laneEnds.findIndex((end) => end <= item.start);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(item.end);
    } else {
      laneEnds[lane] = item.end;
    }
    item.lane = lane;
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.end);
  }
  closeCluster();
  return items;
}

interface Column {
  key: string;
  date: string;
  title: string;
  subtitle: string;
  isToday: boolean;
  color?: string;
  items: StaffAppointment[];
  off: Range[];
}

interface Props {
  token: string;
  clinic: ClinicInfo | null;
  onUnauthorized: () => void;
}

export function StaffCalendar({ token, clinic, onUnauthorized }: Props) {
  const [view, setView] = useState<View>("week");
  const [anchor, setAnchor] = useState(londonToday);
  const [appointments, setAppointments] = useState<StaffAppointment[]>([]);
  const [hidden, setHidden] = useState<Set<number>>(() => new Set());
  const [showCancelled, setShowCancelled] = useState(false);
  const [selected, setSelected] = useState<StaffAppointment | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, setTick] = useState(0);

  const days = useMemo(
    () => (view === "week" ? Array.from({ length: 7 }, (_, i) => addDays(addDays(anchor, -weekdayOf(anchor)), i)) : [anchor]),
    [view, anchor],
  );
  const from = days[0];
  const to = days[days.length - 1];

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.staff<{ appointments: StaffAppointment[] }>(`/appointments?date_from=${from}&date_to=${to}`, token);
      setAppointments(data.appointments);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized();
      else setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [from, to, token, onUnauthorized]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  // Keep the "now" line moving.
  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selected) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  const clinicians = useMemo(() => clinic?.clinicians ?? [], [clinic]);
  const colorOf = useCallback(
    (id: number) => {
      const index = clinicians.findIndex((c) => c.id === id);
      return COLORS[(index >= 0 ? index : clinicians.length) % COLORS.length];
    },
    [clinicians],
  );

  const visible = appointments.filter((a) => !hidden.has(a.clinician_id) && (showCancelled || a.status === "booked"));

  // Show the clinic's opening hours (stretched if anything falls outside them).
  const [startMin, endMin] = useMemo(() => {
    const open = (clinic?.opening_hours ?? []).filter((d) => d.open && d.close);
    let start = open.length ? Math.min(...open.map((d) => hhmmToMinutes(d.open!))) : 8 * 60;
    let end = open.length ? Math.max(...open.map((d) => hhmmToMinutes(d.close!))) : 19 * 60;
    for (const a of appointments) {
      start = Math.min(start, minutesOf(a.start_time));
      end = Math.max(end, minutesOf(a.end_time));
    }
    return [Math.floor(start / 60) * 60, Math.ceil(end / 60) * 60];
  }, [clinic, appointments]);

  const today = londonToday();
  const nowMin = londonNowMinutes();
  const heightPx = ((endMin - startMin) / 60) * HOUR_PX;
  const y = (minutes: number) => ((minutes - startMin) / 60) * HOUR_PX;
  const hourMarks = Array.from({ length: (endMin - startMin) / 60 }, (_, i) => startMin + i * 60);

  const columns: Column[] =
    view === "week"
      ? days.map((date) => {
          const hours = clinic?.opening_hours.find((d) => d.weekday === weekdayOf(date));
          const windows: Range[] = hours?.open && hours.close ? [[hhmmToMinutes(hours.open), hhmmToMinutes(hours.close)]] : [];
          return {
            key: date,
            date,
            title: formatDate(date, { weekday: "short" }),
            subtitle: formatDate(date, { month: "short" }),
            isToday: date === today,
            items: visible.filter((a) => datePart(a.start_time) === date),
            off: offHours(windows, startMin, endMin),
          };
        })
      : clinicians
          .filter((c) => !hidden.has(c.id))
          .map((c) => {
            const windows: Range[] = c.working_hours
              .filter((w) => w.weekday === weekdayOf(anchor))
              .map((w) => [hhmmToMinutes(w.start), hhmmToMinutes(w.end)]);
            return {
              key: String(c.id),
              date: anchor,
              title: c.name,
              subtitle: c.role,
              isToday: anchor === today,
              color: colorOf(c.id),
              items: visible.filter((a) => a.clinician_id === c.id && datePart(a.start_time) === anchor),
              off: offHours(windows, startMin, endMin),
            };
          });

  const step = view === "week" ? 7 : 1;
  const title =
    view === "week" ? weekTitle(days[0], days[6]) : formatDate(anchor, { weekday: "long", day: "numeric", month: "long", year: "numeric" });

  const toggleClinician = (id: number) =>
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <section className="card cal" aria-label="Appointments calendar">
      <div className="cal__toolbar">
        <div className="cal__nav">
          <button
            type="button"
            className="btn btn--ghost btn--sm btn--icon"
            aria-label={view === "week" ? "Previous week" : "Previous day"}
            onClick={() => setAnchor(addDays(anchor, -step))}
          >
            <ChevronLeft size={16} />
          </button>
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => setAnchor(londonToday())}>
            Today
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm btn--icon"
            aria-label={view === "week" ? "Next week" : "Next day"}
            onClick={() => setAnchor(addDays(anchor, step))}
          >
            <ChevronRight size={16} />
          </button>
          <h2 className="cal__title">{title}</h2>
        </div>
        <div className="cal__controls">
          <label className="cal__toggle">
            <input type="checkbox" checked={showCancelled} onChange={(e) => setShowCancelled(e.target.checked)} />
            Show cancelled
          </label>
          <div className="tabs" role="tablist" aria-label="Calendar view">
            {(["week", "day"] as const).map((option) => (
              <button key={option} type="button" role="tab" aria-selected={view === option} onClick={() => setView(option)}>
                {option === "week" ? "Week" : "Day"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {clinicians.length > 0 && (
        <div className="cal__legend" aria-label="Show or hide clinicians">
          {clinicians.map((c) => (
            <button
              key={c.id}
              type="button"
              className="cal__chip"
              style={{ "--c": colorOf(c.id) } as CSSProperties}
              aria-pressed={!hidden.has(c.id)}
              onClick={() => toggleClinician(c.id)}
            >
              <span className="cal__dot" />
              {c.name}
            </button>
          ))}
        </div>
      )}

      {error && <div className="cal__status notice notice--error">{error}</div>}

      <div className="cal__scroll">
        <div
          className="cal__grid"
          style={{ gridTemplateColumns: `64px repeat(${Math.max(columns.length, 1)}, minmax(${view === "week" ? 110 : 170}px, 1fr))` }}
        >
          <div className="cal__corner" />
          {columns.map((col) => (
            <div key={col.key} className={`cal__head${view === "week" && col.isToday ? " is-today" : ""}`}>
              {view === "week" ? (
                <>
                  {col.title} <span className="cal__daynum">{formatDate(col.date, { day: "numeric" })}</span>
                  <small>{col.subtitle}</small>
                </>
              ) : (
                <>
                  <span className="cal__dot" style={{ "--c": col.color, display: "inline-block", marginRight: 6 } as CSSProperties} />
                  {col.title}
                  <small>{col.subtitle}</small>
                </>
              )}
            </div>
          ))}

          <div className="cal__gutter" style={{ height: heightPx }}>
            {hourMarks.map((m, i) => (
              <span key={m} className="cal__hour" style={i === 0 ? { top: 4, transform: "none" } : { top: y(m) }}>
                {clock(m)}
              </span>
            ))}
          </div>

          {columns.map((col) => (
            <div key={col.key} className="cal__col" style={{ height: heightPx, "--hour": `${HOUR_PX}px` } as CSSProperties}>
              {col.off.map(([s, e]) => (
                <div key={s} className="cal__off" style={{ top: y(s), height: y(e) - y(s) }} />
              ))}
              {layoutLanes(col.items).map((placed) => {
                const a = placed.appointment;
                const top = y(placed.start);
                const height = Math.max(y(placed.end) - top - 2, 20);
                return (
                  <button
                    key={a.id}
                    type="button"
                    className={`cal__event${a.status === "cancelled" ? " is-cancelled" : ""}`}
                    style={
                      {
                        top,
                        height,
                        left: `calc(${(placed.lane * 100) / placed.lanes}% + 2px)`,
                        width: `calc(${100 / placed.lanes}% - 4px)`,
                        "--c": colorOf(a.clinician_id),
                      } as CSSProperties
                    }
                    title={`${clock(placed.start)} ${a.patient.name} - ${a.service} (${a.clinician})`}
                    onClick={() => setSelected(a)}
                  >
                    <strong>
                      {clock(placed.start)} {a.patient.name}
                    </strong>
                    {height > 34 && <span>{a.service}</span>}
                    {view === "week" && height > 50 && <span>{a.clinician}</span>}
                  </button>
                );
              })}
              {col.isToday && nowMin >= startMin && nowMin <= endMin && <div className="cal__now" style={{ top: y(nowMin) }} />}
            </div>
          ))}
        </div>
      </div>

      <div className="cal__status">
        {loading ? "Loading…" : `${visible.length} appointment${visible.length === 1 ? "" : "s"} shown`}
        {visible.length > 0 && !loading ? " · click an appointment for details" : ""}
      </div>

      {selected && <AppointmentDialog appointment={selected} color={colorOf(selected.clinician_id)} onClose={() => setSelected(null)} />}
    </section>
  );
}

function AppointmentDialog({ appointment: a, color, onClose }: { appointment: StaffAppointment; color: string; onClose: () => void }) {
  const dob = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${a.patient.date_of_birth}T00:00:00Z`),
  );
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="card dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="appointment-title"
        style={{ borderTop: `4px solid ${color}` }}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="dialog__head">
          <div>
            <div id="appointment-title" className="dialog__title">
              {a.service}
            </div>
            <div className="muted">
              {longDate(a.start_time)} · {timeLabel(a.start_time)} – {timeLabel(a.end_time)}
            </div>
          </div>
          <button type="button" className="btn btn--ghost btn--sm btn--icon" aria-label="Close" onClick={onClose} autoFocus>
            <X size={16} />
          </button>
        </div>
        <dl className="facts">
          <dt>Patient</dt>
          <dd>{a.patient.name}</dd>
          <dt>Date of birth</dt>
          <dd>{dob}</dd>
          <dt>Phone</dt>
          <dd>
            <a href={`tel:${a.patient.phone}`}>{a.patient.phone}</a>
          </dd>
          <dt>Clinician</dt>
          <dd>{a.clinician}</dd>
          <dt>Reference</dt>
          <dd className="mono">{a.reference}</dd>
          <dt>Fee</dt>
          <dd>{a.price}</dd>
          <dt>Status</dt>
          <dd>
            <span className={`badge ${a.status === "booked" ? "badge--success" : ""}`}>{a.status}</span>
          </dd>
          {a.cancellation_reason && (
            <>
              <dt>Reason</dt>
              <dd>{a.cancellation_reason}</dd>
            </>
          )}
          <dt>Google Calendar</dt>
          <dd>{a.calendar_sync_status.replace("_", " ")}</dd>
          <dt>Booked</dt>
          <dd>
            {dayLabel(a.created_at)}, {timeLabel(a.created_at)} by the AI receptionist
          </dd>
        </dl>
      </div>
    </div>
  );
}
