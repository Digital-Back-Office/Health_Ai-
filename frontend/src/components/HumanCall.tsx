import { ArrowLeft, Headset, LoaderCircle, Mic, MicOff, Phone, PhoneCall, PhoneOff, Siren, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { DoctorCallStatus } from "../lib/types";
import { CALL_999, loadVoiceSdk, type HumanCall } from "../lib/useHumanCall";
import { Elapsed } from "./Elapsed";

const DOCTOR_STATUS: Record<string, { label: string; tone: string }> = {
  queued: { label: "Calling…", tone: "badge--brand" },
  ringing: { label: "Ringing…", tone: "badge--brand" },
  answered: { label: "Answered", tone: "badge--brand" },
  accepted: { label: "Joining…", tone: "badge--success" },
  joined: { label: "On the call", tone: "badge--success" },
  left: { label: "Left the call", tone: "" },
  declined: { label: "Can't join", tone: "" },
  "no-answer": { label: "No answer", tone: "" },
  busy: { label: "Busy", tone: "" },
  failed: { label: "Couldn't call", tone: "" },
  canceled: { label: "Stopped", tone: "" },
  completed: { label: "No answer", tone: "" },
};

function names(doctors: DoctorCallStatus[]): string {
  const list = doctors.map((d) => d.name);
  return list.length <= 1 ? (list[0] ?? "") : `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

/** Confirmation before ringing the doctors: 999 always comes first. */
export function SosSheet({
  onClose,
  onConfirm,
  emergencyLine,
  assistant,
}: {
  onClose: () => void;
  onConfirm: () => void;
  emergencyLine: boolean;
  assistant: string;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    dialogRef.current?.focus();
    void loadVoiceSdk().catch(() => {}); // warm up, so the call connects faster
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Portalled to <body> so no sticky/overflow container can trap or cover it.
  return createPortal(
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        ref={dialogRef}
        className="card dialog sos-sheet"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="sos-title"
        aria-describedby="sos-desc"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="dialog__head">
          <div className="sos-sheet__title">
            <span className="sos-sheet__icon">
              <Siren size={20} />
            </span>
            <h2 id="sos-title" className="dialog__title">
              Emergency help
            </h2>
          </div>
          <button type="button" className="btn btn--ghost btn--sm btn--icon" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </div>

        <a className="sos-999" href="tel:999">
          <Phone size={22} />
          <span>
            <strong>Life-threatening? Call 999 now</strong>
            <small>For example chest pain, severe breathing difficulty, heavy bleeding, collapse or signs of a stroke.</small>
          </span>
        </a>

        {emergencyLine ? (
          <>
            <p id="sos-desc" className="muted">
              Need one of our doctors urgently? We'll ring every doctor on call at the same time and connect you on a call
              from this page. {assistant} won't be on the call.
            </p>
            <button type="button" className="btn btn--danger btn--lg" onClick={onConfirm}>
              <PhoneCall size={19} /> Call our doctors now
            </button>
            <p className="subtle">Uses your microphone. For urgent help that isn't an emergency, call NHS 111.</p>
          </>
        ) : (
          <p id="sos-desc" className="muted">
            Our doctors' emergency line isn't available right now. For urgent medical help that isn't an emergency, call{" "}
            <a href="tel:111">NHS 111</a>.
          </p>
        )}
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>,
    document.body,
  );
}

function headline(call: HumanCall): { title: string; lead: string } {
  const joined = call.doctors.filter((d) => d.status === "joined");
  if (call.kind === "reception") {
    switch (call.phase) {
      case "starting":
        return { title: "Calling reception…", lead: "Please allow your microphone if your browser asks." };
      case "ringing":
      case "connected":
        return { title: "Calling reception", lead: "You'll hear ringing until someone at reception answers." };
      case "error":
        return { title: "We couldn't connect the call", lead: call.error ?? "" };
      default:
        return { title: "Call ended", lead: "Thanks for calling. You can start a new conversation at any time." };
    }
  }
  switch (call.phase) {
    case "starting":
      return { title: "Connecting you to our doctors…", lead: "Please allow your microphone if your browser asks." };
    case "ringing": {
      const count = call.doctors.length;
      const who = count ? `${count} doctor${count === 1 ? "" : "s"}` : "our doctors";
      return {
        title: "Calling our doctors…",
        lead: `We're ringing ${who} at the same time. Please stay on the line: you'll hear them as soon as one joins.`,
      };
    }
    case "connected":
      return {
        title: joined.length ? `You're through to ${names(joined)}` : "A doctor is joining…",
        lead: "Speak normally. Other doctors may join the call too.",
      };
    case "unanswered":
      return { title: "None of our doctors could answer", lead: CALL_999 };
    case "error":
      return { title: "We couldn't connect the call", lead: call.error ?? CALL_999 };
    default:
      return { title: "Call ended", lead: "If you still need help, call 999 in an emergency, or NHS 111 for urgent advice." };
  }
}

/** Replaces the AI hero while the patient is on a call with people. */
export function HumanCallPanel({ call, assistant }: { call: HumanCall; assistant: string }) {
  const sos = call.kind === "sos";
  const active = call.phase === "starting" || call.phase === "ringing" || call.phase === "connected";
  const { title, lead } = headline(call);
  const showDoctors = sos && call.doctors.length > 0 && call.phase !== "starting";

  return (
    <div className={`assistant__hero human-call${sos ? " human-call--sos" : ""}`}>
      <span className={`eyebrow${sos ? " eyebrow--danger" : ""}`}>{sos ? "Emergency call" : "Reception"}</span>

      <div className={`call-icon${sos ? " call-icon--sos" : ""}${active ? "" : " call-icon--still"}`} aria-hidden="true">
        <span className="call-icon__ring" />
        <span className="call-icon__ring" />
        <span className="call-icon__core">
          {call.phase === "starting" ? (
            <LoaderCircle size={34} className="spin" />
          ) : sos ? (
            <Siren size={34} />
          ) : (
            <Headset size={34} />
          )}
        </span>
      </div>

      <div className="human-call__text" role="status" aria-live="assertive">
        <h1 id="assistant-title" className="assistant__title">
          {title}
        </h1>
        {lead && <p className="assistant__lead">{lead}</p>}
      </div>

      {active && call.since && (
        <div className="assistant__status assistant__status--live">
          <span className="dot dot--pulse" /> {sos && call.phase !== "connected" ? "Waiting for a doctor" : "On the call"}{" "}
          <Elapsed since={call.since} />
        </div>
      )}

      {showDoctors && (
        <ul className="doctors" aria-label="Doctors we're calling">
          {call.doctors.map((doctor, index) => {
            const status = DOCTOR_STATUS[doctor.status] ?? { label: doctor.status, tone: "" };
            return (
              <li key={`${doctor.name}-${index}`}>
                <span>{doctor.name}</span>
                <span className={`badge ${status.tone}`}>{status.label}</span>
              </li>
            );
          })}
        </ul>
      )}

      <div className="assistant__controls">
        {active ? (
          <>
            <button
              type="button"
              className={`btn btn--ghost btn--lg${call.muted ? " btn--toggled" : ""}`}
              onClick={call.toggleMute}
              aria-pressed={call.muted}
              disabled={!call.since}
            >
              {call.muted ? <MicOff size={18} /> : <Mic size={18} />}
              {call.muted ? "Unmute" : "Mute"}
            </button>
            <button type="button" className="btn btn--danger btn--lg" onClick={call.hangUp}>
              <PhoneOff size={18} /> Hang up
            </button>
          </>
        ) : (
          <>
            {sos && call.phase !== "ended" && (
              <a className="btn btn--danger btn--lg" href="tel:999">
                <Phone size={18} /> Call 999
              </a>
            )}
            <button type="button" className="btn btn--ghost btn--lg" onClick={call.reset}>
              <ArrowLeft size={18} /> Back to {assistant}
            </button>
          </>
        )}
      </div>

      {sos && active && (
        <a className="call-999-line" href="tel:999">
          <Phone size={15} /> Life-threatening? Hang up and call 999
        </a>
      )}
    </div>
  );
}
