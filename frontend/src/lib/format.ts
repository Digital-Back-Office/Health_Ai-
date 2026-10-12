import type { Appointment } from "./types";

const TZ = "Europe/London";

export function dayLabel(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: TZ }).format(new Date(iso));
}

export function longDate(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: TZ }).format(new Date(iso));
}

export function timeLabel(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", { hour: "numeric", minute: "2-digit", hour12: true, timeZone: TZ })
    .format(new Date(iso))
    .replace(" ", "")
    .toLowerCase();
}

export function hhmmToLabel(value: string | null): string {
  if (!value) return "";
  const [h, m] = value.split(":").map(Number);
  const suffix = h < 12 ? "am" : "pm";
  const hour = h % 12 || 12;
  return m ? `${hour}:${String(m).padStart(2, "0")}${suffix}` : `${hour}${suffix}`;
}

export function firstName(fullName: string): string {
  const parts = fullName.split(" ");
  return parts[0] === "Dr" ? `Dr ${parts[parts.length - 1]}` : parts[0];
}

function icsDate(iso: string): string {
  return new Date(iso).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
}

/** Download an .ics file so the patient can add the appointment to their own calendar. */
export function downloadIcs(appointment: Appointment, clinicName: string, address: string): void {
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Health AI//AI Receptionist//EN",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
    "BEGIN:VEVENT",
    `UID:${appointment.reference}@healthai.example`,
    `DTSTAMP:${icsDate(new Date().toISOString())}`,
    `DTSTART:${icsDate(appointment.start_time)}`,
    `DTEND:${icsDate(appointment.end_time)}`,
    `SUMMARY:${appointment.service} - ${clinicName}`,
    `LOCATION:${address.replace(/,/g, "\\,")}`,
    `DESCRIPTION:With ${appointment.clinician}. Booking reference ${appointment.reference}.${
      appointment.preparation ? `\\n${appointment.preparation.replace(/,/g, "\\,")}` : ""
    }`,
    "BEGIN:VALARM",
    "TRIGGER:-PT2H",
    "ACTION:DISPLAY",
    "DESCRIPTION:Appointment reminder",
    "END:VALARM",
    "END:VEVENT",
    "END:VCALENDAR",
  ];
  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `appointment-${appointment.reference}.ics`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

export function mapsLink(address: string): string {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(address)}`;
}
