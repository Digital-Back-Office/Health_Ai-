import type { ClinicInfo, SessionState, StartedSession } from "./types";

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
  endSession: (session: StartedSession) => {
    const url = `/api/sessions/${session.session_id}/end`;
    const body = JSON.stringify({ token: session.session_token });
    if (navigator.sendBeacon?.(url, new Blob([body], { type: "application/json" }))) return;
    void fetch(url, { method: "POST", body, headers: { "Content-Type": "application/json" }, keepalive: true }).catch(() => {});
  },

  staff: <T,>(path: string, token: string, init: RequestInit = {}) => request<T>(`/api/admin${path}`, init, token),
};
