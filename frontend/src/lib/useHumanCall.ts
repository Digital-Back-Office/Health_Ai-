/**
 * Calls with people, not the AI: the SOS call with the clinic's doctors, or a call to reception.
 * The browser joins over WebRTC with Twilio's Voice SDK; the backend's TwiML app puts it into the
 * right call (the SOS conference, or a <Dial> to the reception number).
 */
import type { Call, Device } from "@twilio/voice-sdk";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type SessionAuth } from "./api";
import { subscribeToEvents, type SseHandle } from "./sse";
import type { DoctorCallStatus, SosPayload, SosStarted } from "./types";

export type HumanCallKind = "sos" | "reception";
export type HumanCallPhase = "idle" | "starting" | "ringing" | "connected" | "unanswered" | "ended" | "error";

export interface HumanCallState {
  kind: HumanCallKind | null;
  phase: HumanCallPhase;
  doctors: DoctorCallStatus[];
  muted: boolean;
  /** When the browser's call connected (for the timer). */
  since: number | null;
  error: string | null;
}

const IDLE: HumanCallState = { kind: null, phase: "idle", doctors: [], muted: false, since: null, error: null };

export const CALL_999 = "If this is life-threatening, call 999 now. For urgent medical help, call NHS 111.";

const MIC_NEEDED: Record<HumanCallKind, string> = {
  sos: `We need your microphone to connect you to a doctor. Please allow microphone access and try again. ${CALL_999}`,
  reception: "We need your microphone to call reception. Please allow microphone access, or phone the clinic instead.",
};

const CANT_CONNECT: Record<HumanCallKind, string> = {
  sos: `We couldn't connect the call from your browser. ${CALL_999}`,
  reception: "We couldn't connect you to reception from your browser. Please phone the clinic instead.",
};

// When nobody answers, the backend plays a "call 999" message and hangs up; this is the fallback.
const UNANSWERED_GRACE_MS = 15_000;

type VoiceSdk = typeof import("@twilio/voice-sdk");
let sdkPromise: Promise<VoiceSdk> | null = null;

/** The Twilio SDK is only needed for calls with people, so it's loaded on demand. */
export function loadVoiceSdk(): Promise<VoiceSdk> {
  sdkPromise ??= import("@twilio/voice-sdk").catch((error: unknown) => {
    sdkPromise = null;
    throw error;
  });
  return sdkPromise;
}

async function microphoneAllowed(): Promise<boolean> {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((track) => track.stop());
    return true;
  } catch {
    return false;
  }
}

function nextPhase(current: HumanCallPhase, status: SosPayload["status"]): HumanCallPhase {
  if (current === "idle" || current === "ended" || current === "error") return current;
  if (status === "connected") return "connected";
  if (status === "unanswered" || status === "failed") return "unanswered";
  return current;
}

export function useHumanCall() {
  const [state, setState] = useState<HumanCallState>(IDLE);
  // Incremented by every new call and every hang-up: lets in-flight work notice it's stale.
  const attemptRef = useRef(0);
  const authRef = useRef<SessionAuth | null>(null);
  const incidentRef = useRef<string | null>(null);
  const deviceRef = useRef<Device | null>(null);
  const callRef = useRef<Call | null>(null);
  const sseRef = useRef<SseHandle | null>(null);
  const graceRef = useRef<number | null>(null);

  /** Drop the call and its event stream, stop ringing the doctors (SOS), then release the session. */
  const release = useCallback(() => {
    if (graceRef.current) window.clearTimeout(graceRef.current);
    graceRef.current = null;
    sseRef.current?.close();
    sseRef.current = null;
    const call = callRef.current;
    const device = deviceRef.current;
    callRef.current = null;
    deviceRef.current = null;
    try {
      call?.disconnect();
      device?.destroy();
    } catch {
      /* already gone */
    }
    const auth = authRef.current;
    const incident = incidentRef.current;
    authRef.current = null;
    incidentRef.current = null;
    if (!auth) return;
    if (incident) {
      void api
        .endSos(auth, incident)
        .catch(() => {})
        .finally(() => api.endSession(auth));
    } else {
      api.endSession(auth);
    }
  }, []);

  const finish = useCallback(
    (outcome: "ended" | "unanswered" | "error", error?: string) => {
      attemptRef.current += 1;
      release();
      setState((s) => ({
        ...s,
        phase: outcome === "ended" && s.phase === "unanswered" ? "unanswered" : outcome,
        error: error ?? s.error,
        muted: false,
      }));
    },
    [release],
  );

  /** Start a new call attempt (replacing anything left over) and return its id. */
  const begin = useCallback(
    (kind: HumanCallKind, doctors: DoctorCallStatus[] = []) => {
      attemptRef.current += 1;
      release();
      setState({ ...IDLE, kind, phase: "starting", doctors });
      void loadVoiceSdk().catch(() => {});
      return attemptRef.current;
    },
    [release],
  );

  const onSosEvent = useCallback(
    (type: string, payload: SosPayload) => {
      if (!payload || payload.incident_id !== incidentRef.current) return;
      setState((s) => ({
        ...s,
        doctors: payload.doctors?.length ? payload.doctors : s.doctors,
        phase: nextPhase(s.phase, payload.status),
      }));
      if (type === "sos_ended" || payload.status === "ended" || payload.status === "failed") {
        finish(payload.status === "failed" || payload.status === "unanswered" ? "unanswered" : "ended");
      } else if (payload.status === "unanswered" && !graceRef.current) {
        graceRef.current = window.setTimeout(() => finish("unanswered"), UNANSWERED_GRACE_MS);
      }
    },
    [finish],
  );

  /** Connect this browser to the call (the session is ours from here on). */
  const join = useCallback(
    async (attempt: number, kind: HumanCallKind, auth: SessionAuth, incidentId: string | null) => {
      const stale = () => attempt !== attemptRef.current;
      authRef.current = auth;
      incidentRef.current = incidentId;
      if (incidentId) {
        sseRef.current = subscribeToEvents(`/api/sessions/${auth.session_id}/events`, auth.session_token, (type, data) => {
          if (type.startsWith("sos_")) onSosEvent(type, data as SosPayload);
        });
        api
          .sosStatus(auth, incidentId)
          .then((payload) => !stale() && onSosEvent("sos_update", payload))
          .catch(() => {});
      }

      try {
        const [{ Call: TwilioCall, Device: TwilioDevice }, token] = await Promise.all([
          loadVoiceSdk(),
          api.voiceToken(auth, kind),
        ]);
        if (stale()) return;
        if (!TwilioDevice.isSupported) {
          finish("error", CANT_CONNECT[kind]);
          return;
        }
        const device = new TwilioDevice(token.token, {
          edge: token.edge || undefined,
          codecPreferences: [TwilioCall.Codec.Opus, TwilioCall.Codec.PCMU],
          closeProtection: kind === "sos" ? "You're on an emergency call. Leaving this page will hang up." : true,
          logLevel: "error",
        });
        deviceRef.current = device;
        device.on("tokenWillExpire", () => {
          api
            .voiceToken(auth, kind)
            .then((next) => device.updateToken(next.token))
            .catch(() => {});
        });

        const call = await device.connect({ params: incidentId ? { purpose: kind, incident: incidentId } : { purpose: kind } });
        if (stale()) {
          call.disconnect();
          return;
        }
        callRef.current = call;
        call.on("accept", () => {
          if (stale()) return;
          setState((s) => ({
            ...s,
            since: s.since ?? Date.now(),
            phase: s.phase === "starting" ? (kind === "sos" ? "ringing" : "connected") : s.phase,
          }));
        });
        call.on("mute", (muted: boolean) => {
          if (!stale()) setState((s) => ({ ...s, muted }));
        });
        const ended = () => {
          if (!stale()) finish("ended");
        };
        call.on("disconnect", ended);
        call.on("cancel", ended);
        call.on("reject", ended);
        // Some errors are recovered from (the SDK reconnects); only give up once the call is closed.
        const failIfClosed = () => {
          if (!stale() && call.status() === TwilioCall.State.Closed) finish("error", CANT_CONNECT[kind]);
        };
        call.on("error", () => {
          failIfClosed();
          window.setTimeout(failIfClosed, 2000);
        });
      } catch {
        if (!stale()) finish("error", CANT_CONNECT[kind]);
      }
    },
    [finish, onSosEvent],
  );

  /**
   * The SOS button. `session` is the conversation being handed over, or null to start straight
   * from the button. The microphone is checked first, so doctors are never rung for a patient who
   * can't talk to them.
   */
  const startSos = useCallback(
    async (session: Promise<SessionAuth | null>, reason?: string) => {
      const attempt = begin("sos");
      const stale = () => attempt !== attemptRef.current;
      const [micOk, existing] = await Promise.all([microphoneAllowed(), session]);
      if (stale() || !micOk) {
        if (existing) api.endSession(existing);
        if (!stale()) finish("error", MIC_NEEDED.sos);
        return;
      }
      let started: SosStarted;
      try {
        started = await api.startSos(existing, reason);
      } catch (error) {
        if (existing) api.endSession(existing);
        if (!stale()) {
          finish(
            "error",
            error instanceof ApiError && error.status >= 400 ? error.message : `We couldn't start the emergency call. ${CALL_999}`,
          );
        }
        return;
      }
      const auth: SessionAuth | null =
        existing ??
        (started.session_id && started.session_token
          ? { session_id: started.session_id, session_token: started.session_token }
          : null);
      if (!auth) {
        if (!stale()) finish("error", CANT_CONNECT.sos);
        return;
      }
      if (stale()) {
        // Hung up while we were setting up: don't leave the doctors ringing.
        void api
          .endSos(auth, started.incident_id)
          .catch(() => {})
          .finally(() => api.endSession(auth));
        return;
      }
      await join(attempt, "sos", auth, started.incident_id);
    },
    [begin, finish, join],
  );

  /** Sophie started the SOS call (the patient said it's an emergency): join it. */
  const joinSos = useCallback(
    async (session: Promise<SessionAuth | null>, incident: SosPayload) => {
      const attempt = begin("sos", incident.doctors);
      const stale = () => attempt !== attemptRef.current;
      const [micOk, auth] = await Promise.all([microphoneAllowed(), session]);
      if (!auth) {
        if (!stale()) finish("error", CANT_CONNECT.sos);
        return;
      }
      if (stale() || !micOk) {
        void api
          .endSos(auth, incident.incident_id)
          .catch(() => {})
          .finally(() => api.endSession(auth));
        if (!stale()) finish("error", MIC_NEEDED.sos);
        return;
      }
      await join(attempt, "sos", auth, incident.incident_id);
    },
    [begin, finish, join],
  );

  /** Sophie is putting the patient through to reception. */
  const callReception = useCallback(
    async (session: Promise<SessionAuth | null>) => {
      const attempt = begin("reception");
      const stale = () => attempt !== attemptRef.current;
      const [micOk, auth] = await Promise.all([microphoneAllowed(), session]);
      if (!auth) {
        if (!stale()) finish("error", CANT_CONNECT.reception);
        return;
      }
      if (stale() || !micOk) {
        api.endSession(auth);
        if (!stale()) finish("error", MIC_NEEDED.reception);
        return;
      }
      await join(attempt, "reception", auth, null);
    },
    [begin, finish, join],
  );

  const hangUp = useCallback(() => finish("ended"), [finish]);

  /** Back to the AI receptionist screen. */
  const reset = useCallback(() => {
    attemptRef.current += 1;
    release();
    setState(IDLE);
  }, [release]);

  const toggleMute = useCallback(() => {
    const call = callRef.current;
    if (call) call.mute(!call.isMuted());
  }, []);

  useEffect(
    () => () => {
      attemptRef.current += 1;
      release();
    },
    [release],
  );

  return { ...state, startSos, joinSos, callReception, hangUp, reset, toggleMute };
}

export type HumanCall = ReturnType<typeof useHumanCall>;
