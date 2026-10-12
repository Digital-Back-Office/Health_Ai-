import { useConversation } from "@elevenlabs/react";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, type SessionAuth } from "./api";
import { subscribeToEvents, type SseHandle } from "./sse";
import type { SessionEvent, SosPayload, StartedSession, TranscriptEntry } from "./types";
import { initialVisit, visitReducer } from "./visit";

export type Phase = "idle" | "preparing" | "connecting" | "live" | "ended" | "error";
export type Mode = "voice" | "text";

/** Sophie is handing the patient to a person; `session` resolves once she has finished speaking. */
export type HumanRequest =
  | { kind: "sos"; incident: SosPayload; session: Promise<SessionAuth | null> }
  | { kind: "reception"; session: Promise<SessionAuth | null> };

export interface ReceptionistOptions {
  onHumanRequest?: (request: HumanRequest) => void;
}

// How long to let Sophie finish her last sentence before the call is handed over.
const HANDOVER_WAIT_MS = { sos: 7000, reception: 9000 };

let entryCounter = 0;
const newId = () => `m${Date.now().toString(36)}${(entryCounter++).toString(36)}`;

function friendlyError(message: string): string {
  const lower = message.toLowerCase();
  if (lower.includes("permission") || lower.includes("notallowed") || lower.includes("microphone")) {
    return "We couldn't use your microphone. Please allow microphone access in your browser, or type your message instead.";
  }
  if (lower.includes("quota") || lower.includes("limit") || lower.includes("capacity")) {
    return "Our assistant is very busy right now. Please try again in a minute, or call the clinic.";
  }
  return message || "Something went wrong with the connection. Please try again.";
}

/**
 * Everything the patient-facing page needs for one conversation:
 * backend session -> ElevenLabs connection -> transcript, live visit events, heartbeat.
 */
export function useReceptionist(options: ReceptionistOptions = {}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [mode, setMode] = useState<Mode>("voice");
  const [error, setError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [agentTyping, setAgentTyping] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [visit, dispatch] = useReducer(visitReducer, initialVisit);

  const sessionRef = useRef<StartedSession | null>(null);
  const sseRef = useRef<SseHandle | null>(null);
  const heartbeatRef = useRef<number | null>(null);
  const pendingMessageRef = useRef<string | null>(null);
  const connectedRef = useRef(false);
  // Incremented by every start() and end(): lets an in-flight start() notice it was cancelled.
  const attemptRef = useRef(0);
  const sendRef = useRef<(text: string) => void>(() => {});
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const speakingRef = useRef(false);
  const agentTypingRef = useRef(false);
  const lastAgentMessageRef = useRef(0);
  const handOverRef = useRef<(waitMs: number) => Promise<SessionAuth | null>>(async () => null);

  const addEntry = useCallback((role: TranscriptEntry["role"], text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    setTranscript((items) => [...items, { id: newId(), role, text: trimmed, at: Date.now() }]);
  }, []);

  const teardown = useCallback(() => {
    if (heartbeatRef.current) window.clearInterval(heartbeatRef.current);
    heartbeatRef.current = null;
    sseRef.current?.close();
    sseRef.current = null;
    const session = sessionRef.current;
    sessionRef.current = null;
    connectedRef.current = false;
    setAgentTyping(false);
    if (session) api.endSession(session);
  }, []);

  const conversation = useConversation({
    onConnect: ({ conversationId }) => {
      connectedRef.current = true;
      setPhase("live");
      setStartedAt(Date.now());
      const session = sessionRef.current;
      if (session) api.bindConversation(session, conversationId).catch(() => {});
      const pending = pendingMessageRef.current;
      pendingMessageRef.current = null;
      if (pending) window.setTimeout(() => sendRef.current(pending), 600);
    },
    onDisconnect: (details) => {
      const wasLive = connectedRef.current || sessionRef.current !== null;
      teardown();
      if (!wasLive) return;
      if (details.reason === "error") {
        setError(friendlyError(details.message));
        setPhase("error");
      } else {
        setPhase("ended");
      }
    },
    onMessage: ({ message, role }) => {
      addEntry(role, message);
      if (role === "agent") {
        lastAgentMessageRef.current = Date.now();
        agentTypingRef.current = false;
        setAgentTyping(false);
      }
    },
    onModeChange: ({ mode }) => {
      speakingRef.current = mode === "speaking";
    },
    onAgentChatResponsePart: (part) => {
      if (part.type === "start") agentTypingRef.current = true;
      if (part.type === "stop") agentTypingRef.current = false;
      if (part.type === "start") setAgentTyping(true);
      if (part.type === "stop") setAgentTyping(false);
    },
    onError: (message) => {
      setError(friendlyError(message));
      if (!connectedRef.current) {
        teardown();
        setPhase("error");
      }
    },
  });

  const sendText = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      addEntry("user", trimmed);
      conversation.sendUserMessage(trimmed);
    },
    [addEntry, conversation],
  );
  sendRef.current = sendText;

  const start = useCallback(
    async (nextMode: Mode, firstMessage?: string) => {
      if (sessionRef.current) return;
      const attempt = ++attemptRef.current;
      const cancelled = () => attempt !== attemptRef.current;
      setError(null);
      setTranscript([]);
      setStartedAt(null);
      dispatch({ type: "reset" });
      setMode(nextMode);
      setPhase("preparing");
      pendingMessageRef.current = firstMessage ?? null;

      if (nextMode === "voice") {
        try {
          // Ask for the microphone up front so a refusal gets a clear message.
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          stream.getTracks().forEach((track) => track.stop());
        } catch {
          if (cancelled()) return;
          setError(friendlyError("microphone permission denied"));
          setPhase("error");
          return;
        }
        if (cancelled()) return;
      }

      let session: StartedSession;
      try {
        session = await api.startSession(nextMode);
      } catch (err) {
        if (cancelled()) return;
        setError(friendlyError((err as Error).message));
        setPhase("error");
        return;
      }
      if (cancelled()) {
        api.endSession(session); // cancelled while we were waiting: release it straight away
        return;
      }
      sessionRef.current = session;
      sseRef.current = subscribeToEvents(`/api/sessions/${session.session_id}/events`, session.session_token, (type, data) => {
        if (type === "ready") return;
        const { onHumanRequest } = optionsRef.current;
        if (type === "sos_started" && onHumanRequest) {
          onHumanRequest({ kind: "sos", incident: data as SosPayload, session: handOverRef.current(HANDOVER_WAIT_MS.sos) });
          return;
        }
        if (type === "reception_requested" && onHumanRequest) {
          onHumanRequest({ kind: "reception", session: handOverRef.current(HANDOVER_WAIT_MS.reception) });
          return;
        }
        if (type.startsWith("sos_") || type === "reception_requested") return;
        if (type === "state_changed") {
          api.state(session).then((state) => dispatch({ type: "hydrate", state })).catch(() => {});
          return;
        }
        dispatch({ type, data } as SessionEvent);
      });
      heartbeatRef.current = window.setInterval(() => {
        api.heartbeat(session).catch(() => {});
      }, 20_000);

      setPhase("connecting");
      const common = { dynamicVariables: session.dynamic_variables, userId: session.session_id };
      if (nextMode === "voice" && session.conversation_token) {
        conversation.startSession({ conversationToken: session.conversation_token, connectionType: "webrtc", ...common });
      } else if (session.signed_url) {
        conversation.startSession({
          signedUrl: session.signed_url,
          connectionType: "websocket",
          textOnly: true,
          overrides: { conversation: { textOnly: true } },
          ...common,
        });
      } else {
        teardown();
        setError("The assistant couldn't be started. Please try again.");
        setPhase("error");
      }
    },
    [conversation, teardown],
  );

  const end = useCallback(() => {
    attemptRef.current += 1;
    conversation.endSession();
    teardown();
    setPhase((current) => (current === "idle" ? current : "ended"));
  }, [conversation, teardown]);

  /** Resolves once Sophie has said her last sentence (or after maxMs). */
  const agentFinished = useCallback(
    (maxMs: number) =>
      new Promise<void>((resolve) => {
        const since = Date.now();
        let quietFrom = 0;
        const check = () => {
          const now = Date.now();
          if (now - since >= maxMs) return resolve();
          const spoke = lastAgentMessageRef.current >= since;
          if (spoke && !speakingRef.current && !agentTypingRef.current) {
            quietFrom ||= now;
            if (now - quietFrom >= 900) return resolve();
          } else {
            quietFrom = 0;
          }
          window.setTimeout(check, 150);
        };
        check();
      }),
    [],
  );

  /**
   * End the AI conversation but keep the backend session: a call with people (SOS / reception)
   * takes it over. Pass waitMs to let Sophie finish speaking first.
   */
  const handOver = useCallback(
    async (waitMs = 0): Promise<SessionAuth | null> => {
      attemptRef.current += 1; // cancels a start() still in flight
      const session = sessionRef.current;
      sessionRef.current = null; // the call owns the session now, so teardown won't end it
      pendingMessageRef.current = null;
      sseRef.current?.close();
      sseRef.current = null;
      if (heartbeatRef.current) window.clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
      if (session && waitMs > 0 && connectedRef.current) await agentFinished(waitMs);
      connectedRef.current = false;
      if (session) conversation.endSession();
      agentTypingRef.current = false;
      setAgentTyping(false);
      setPhase((current) => (current === "idle" ? current : "ended"));
      return session ? { session_id: session.session_id, session_token: session.session_token } : null;
    },
    [agentFinished, conversation],
  );
  handOverRef.current = handOver;

  // Clear a stale "working on it" indicator if a tool never reports back.
  useEffect(() => {
    if (!visit.activity) return;
    const timer = window.setTimeout(() => dispatch({ type: "clear_activity" }), 20_000);
    return () => window.clearTimeout(timer);
  }, [visit.activity]);

  // Release the conversation slot if the tab is closed mid-call.
  useEffect(() => {
    const onUnload = () => {
      if (sessionRef.current) api.endSession(sessionRef.current);
    };
    window.addEventListener("pagehide", onUnload);
    return () => window.removeEventListener("pagehide", onUnload);
  }, []);

  return {
    phase,
    mode,
    error,
    dismissError: () => setError(null),
    transcript,
    agentTyping,
    startedAt,
    visit,
    start,
    end,
    handOver,
    sendText,
    isSpeaking: conversation.isSpeaking,
    isMuted: conversation.isMuted,
    setMuted: conversation.setMuted,
    getInputVolume: conversation.getInputVolume,
    getOutputVolume: conversation.getOutputVolume,
    sendUserActivity: conversation.sendUserActivity,
  };
}

export type Receptionist = ReturnType<typeof useReceptionist>;
