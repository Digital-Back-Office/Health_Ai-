import type { ClinicInfo, SessionState, SosPayload, SosStarted, StartedSession } from "./types";

/** The parts of a session needed to authenticate as it (AI conversation or SOS-only). */
export interface SessionAuth {
  session_id: string;
  session_token: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError("We couldn't reach the clinic's server. Please check your connection and try again.", 0);
  }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") message = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  clinic: () => request<ClinicInfo>("/api/clinic"),

  startSession: (mode: "voice" | "text") =>
    request<StartedSession>("/api/sessions", { method: "POST", body: JSON.stringify({ mode }) }),

  bindConversation: (session: StartedSession, conversationId: string) =>
    request<void>(
      `/api/sessions/${session.session_id}/conversation`,
      { method: "POST", body: JSON.stringify({ conversation_id: conversationId }) },
      session.session_token,
    ),

  heartbeat: (session: StartedSession) =>
    request<{ expires_at: string }>(`/api/sessions/${session.session_id}/heartbeat`, { method: "POST" }, session.session_token),

  state: (session: StartedSession) =>
    request<SessionState>(`/api/sessions/${session.session_id}/state`, {}, session.session_token),

  /** Best effort, also works while the page is unloading. */
  endSession: (session: SessionAuth) => {
    const url = `/api/sessions/${session.session_id}/end`;
    const body = JSON.stringify({ token: session.session_token });
    if (navigator.sendBeacon?.(url, new Blob([body], { type: "application/json" }))) return;
    void fetch(url, { method: "POST", body, headers: { "Content-Type": "application/json" }, keepalive: true }).catch(() => {});
  },

  /** SOS during a conversation, or (without a session) straight from the SOS button. */
  startSos: (session: SessionAuth | null, reason?: string) =>
    session
      ? request<SosStarted>(
          `/api/sessions/${session.session_id}/sos`,
          { method: "POST", body: JSON.stringify({ reason }) },
          session.session_token,
        )
      : request<SosStarted>("/api/sos", { method: "POST", body: JSON.stringify({ reason }) }),

  sosStatus: (session: SessionAuth, incidentId: string) =>
    request<SosPayload>(`/api/sessions/${session.session_id}/sos/${incidentId}`, {}, session.session_token),

  /** The patient hung up (or couldn't connect): stop ringing the doctors. */
  endSos: (session: SessionAuth, incidentId: string) =>
    request<void>(`/api/sessions/${session.session_id}/sos/${incidentId}/end`, { method: "POST" }, session.session_token),

  /** Short-lived Twilio token so this browser can join the SOS call or call reception. */
  voiceToken: (session: SessionAuth, purpose: "sos" | "reception") =>
    request<{ token: string; identity: string; edge: string }>(
      `/api/sessions/${session.session_id}/voice-token`,
      { method: "POST", body: JSON.stringify({ purpose }) },
      session.session_token,
    ),

  staff: <T,>(path: string, token: string, init: RequestInit = {}) => request<T>(`/api/admin${path}`, init, token),
};
