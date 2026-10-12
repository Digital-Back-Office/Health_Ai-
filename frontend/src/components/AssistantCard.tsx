import {
  CalendarClock,
  CalendarPlus,
  CalendarX,
  Clock,
  Info,
  Keyboard,
  LoaderCircle,
  MessageSquareText,
  Mic,
  MicOff,
  PhoneOff,
  Send,
  Siren,
  Sparkles,
  TriangleAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { ClinicInfo } from "../lib/types";
import type { HumanCall } from "../lib/useHumanCall";
import type { Mode, Receptionist } from "../lib/useReceptionist";
import { Elapsed } from "./Elapsed";
import { HumanCallPanel, SosSheet } from "./HumanCall";
import { VoiceOrb, type OrbState } from "./VoiceOrb";

const SUGGESTIONS = [
  { icon: CalendarPlus, text: "I'd like to book a GP appointment" },
  { icon: CalendarClock, text: "I need to reschedule my appointment" },
  { icon: CalendarX, text: "I want to cancel an appointment" },
  { icon: Clock, text: "What are your opening hours?" },
];

interface Props {
  r: Receptionist;
  clinic: ClinicInfo | null;
  /** A call with people (SOS doctors / reception) that takes over from the AI. */
  human: HumanCall;
  onSos: () => void;
}

export function AssistantCard({ r, clinic, human, onSos }: Props) {
  const [preferredMode, setPreferredMode] = useState<Mode>("voice");
  const [draft, setDraft] = useState("");
  const [sosOpen, setSosOpen] = useState(false);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const lastActivityRef = useRef(0);
  const assistant = clinic?.assistant_name ?? "Sophie";

  const onHumanCall = human.phase !== "idle";
  const busy = r.phase === "preparing" || r.phase === "connecting";
  const live = r.phase === "live";
  const voiceLive = live && r.mode === "voice";
  const closeSos = useCallback(() => setSosOpen(false), []);

  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [r.transcript.length, r.agentTyping]);

  let orbState: OrbState = "idle";
  if (busy) orbState = "connecting";
  else if (live && r.mode === "text") orbState = "text";
  else if (voiceLive && r.isMuted) orbState = "muted";
  else if (voiceLive) orbState = r.isSpeaking ? "speaking" : "listening";

  const statusText = (() => {
    switch (r.phase) {
      case "preparing":
        return r.mode === "voice" ? "Checking your microphone…" : "Getting ready…";
      case "connecting":
        return `Connecting you to ${assistant}…`;
      case "live":
        if (r.mode === "text") return r.agentTyping ? `${assistant} is typing…` : "Chat connected";
        if (r.isMuted) return "Your microphone is muted";
        return r.isSpeaking ? `${assistant} is speaking` : "Listening…";
      case "ended":
        return "Conversation ended";
      case "error":
        return "We couldn't start the conversation";
      default:
        return "Available 24/7 · bookings, changes and questions";
    }
  })();

  const begin = (message?: string) => {
    if (!onHumanCall) void r.start(preferredMode, message);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!draft.trim() || !live) return;
    r.sendText(draft);
    setDraft("");
  };

  const onDraftChange = (value: string) => {
    setDraft(value);
    // While typing during a voice call, ask the agent to hold on rather than talk over.
    if (voiceLive && Date.now() - lastActivityRef.current > 1500) {
      lastActivityRef.current = Date.now();
      r.sendUserActivity();
    }
  };

  return (
    <section className="card assistant" aria-labelledby="assistant-title">
      {onHumanCall ? (
        <HumanCallPanel call={human} assistant={assistant} />
      ) : (
        <div className="assistant__hero">
          <button
            type="button"
            className="sos-btn"
            onClick={() => setSosOpen(true)}
            aria-label="SOS: get emergency help"
            title="Emergency help"
          >
            <Siren size={17} /> SOS
          </button>
          <span className="eyebrow">AI receptionist</span>
          <h1 id="assistant-title" className="assistant__title">
            Hi, I'm {assistant}. How can I help?
          </h1>
          <p className="assistant__lead">
            Book, reschedule or cancel an appointment, or ask about our services, prices and opening hours. Just talk to me
            as you would on the phone.
          </p>

          <VoiceOrb state={orbState} getLevel={r.isSpeaking ? r.getOutputVolume : r.getInputVolume} />

          <div className={`assistant__status${live ? " assistant__status--live" : ""}`} role="status" aria-live="polite">
            {live && <span className="dot dot--pulse" />}
            {statusText}
            {live && r.startedAt && <Elapsed since={r.startedAt} label="Conversation length" />}
          </div>

          <div className="assistant__controls">
            {!live && !busy && (
              <>
                <div className="mode-switch" role="group" aria-label="How would you like to talk?">
                  <button type="button" aria-pressed={preferredMode === "voice"} onClick={() => setPreferredMode("voice")}>
                    <Mic size={15} /> Talk
                  </button>
                  <button type="button" aria-pressed={preferredMode === "text"} onClick={() => setPreferredMode("text")}>
                    <Keyboard size={15} /> Type
                  </button>
                </div>
                <button type="button" className="btn btn--primary btn--lg" onClick={() => begin()}>
                  {preferredMode === "voice" ? <Mic size={19} /> : <MessageSquareText size={19} />}
                  {r.phase === "ended" ? "Start a new conversation" : preferredMode === "voice" ? "Start talking" : "Start chatting"}
                </button>
              </>
            )}
            {busy && (
              <>
                <button type="button" className="btn btn--primary btn--lg" disabled>
                  <LoaderCircle size={19} className="spin" /> Connecting…
                </button>
                <button type="button" className="btn btn--ghost btn--lg" onClick={r.end}>
                  Cancel
                </button>
              </>
            )}
            {live && (
              <>
                {r.mode === "voice" && (
                  <button
                    type="button"
                    className={`btn btn--ghost btn--lg${r.isMuted ? " btn--toggled" : ""}`}
                    onClick={() => r.setMuted(!r.isMuted)}
                    aria-pressed={r.isMuted}
                  >
                    {r.isMuted ? <MicOff size={18} /> : <Mic size={18} />}
                    {r.isMuted ? "Unmute" : "Mute"}
                  </button>
                )}
                <button type="button" className="btn btn--danger btn--lg" onClick={r.end}>
                  <PhoneOff size={18} /> End
                </button>
              </>
            )}
          </div>
        </div>
      )}

      {sosOpen && (
        <SosSheet
          assistant={assistant}
          emergencyLine={clinic?.emergency_line ?? false}
          onClose={closeSos}
          onConfirm={() => {
            setSosOpen(false);
            onSos();
          }}
        />
      )}

      <div className="conversation">
        <div className="transcript" ref={transcriptRef} aria-live="polite" aria-label="Conversation transcript">
          {r.transcript.length === 0 && !r.agentTyping ? (
            <div className="empty">
              <p className="muted">
                <Sparkles size={15} style={{ verticalAlign: "-2px", marginRight: 6 }} />
                {live ? `Say hello to ${assistant}, or tap a suggestion.` : "Not sure where to start? Try one of these:"}
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map(({ icon: Icon, text }) => (
                  <button
                    key={text}
                    type="button"
                    className="chip"
                    disabled={busy || onHumanCall}
                    onClick={() => (live ? r.sendText(text) : begin(text))}
                  >
                    <Icon size={15} /> {text}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            r.transcript.map((entry) => (
              <div key={entry.id} className={`msg msg--${entry.role}`}>
                {entry.role === "agent" && <span className="msg__avatar">{assistant[0]}</span>}
                <div className="msg__bubble">
                  <span className="sr-only">{entry.role === "agent" ? `${assistant}: ` : "You: "}</span>
                  {entry.text}
                </div>
              </div>
            ))
          )}
          {r.agentTyping && (
            <div className="msg msg--agent">
              <span className="msg__avatar">{assistant[0]}</span>
              <div className="msg__bubble typing" aria-label={`${assistant} is typing`}>
                <span />
                <span />
                <span />
              </div>
            </div>
          )}
        </div>

        <div className="assistant__notices">
          {r.error && (
            <div className="notice notice--error" role="alert">
              <TriangleAlert size={18} />
              <span>{r.error}</span>
              <button type="button" onClick={r.dismissError} aria-label="Dismiss">
                <X size={16} />
              </button>
            </div>
          )}
          {r.phase === "idle" && (
            <div className="notice notice--info">
              <Info size={18} />
              <span>
                Prototype: please use test details only. {assistant} is an AI assistant: it can book appointments and answer
                questions about the clinic, but it can't give medical advice. Conversations are processed by our technology
                partners (ElevenLabs and Google Cloud) under UK GDPR.
              </span>
            </div>
          )}
        </div>

        {live && (
          <form className="composer" onSubmit={submit}>
            <label htmlFor="composer-input" className="sr-only">
              Type a message
            </label>
            <input
              id="composer-input"
              value={draft}
              onChange={(e) => onDraftChange(e.target.value)}
              placeholder={r.mode === "text" ? "Type your message…" : "Prefer typing? Write here…"}
              autoComplete="off"
              maxLength={500}
            />
            <button type="submit" className="btn btn--primary btn--icon" aria-label="Send" disabled={!draft.trim()}>
              <Send size={18} />
            </button>
          </form>
        )}
      </div>
    </section>
  );
}
