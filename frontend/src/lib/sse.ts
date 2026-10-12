/**
 * Server-sent events over fetch(), so the session token can travel in the
 * Authorization header (EventSource can't set headers). Reconnects with backoff.
 */
export interface SseHandle {
  close: () => void;
}

export function subscribeToEvents(
  url: string,
  token: string,
  onEvent: (type: string, data: unknown) => void,
  onStatus?: (connected: boolean) => void,
): SseHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;

  const run = async () => {
    while (!closed) {
      try {
        const response = await fetch(url, {
          headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
          signal: controller.signal,
          cache: "no-store",
        });
        if (response.status === 401 || response.status === 403) return; // session over
        if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`);
        attempt = 0;
        onStatus?.(true);

        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let boundary: number;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            let type = "message";
            const data: string[] = [];
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) type = line.slice(6).trim();
              else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
            }
            if (!data.length) continue;
            try {
              onEvent(type, JSON.parse(data.join("\n")));
            } catch {
              /* ignore malformed events */
            }
          }
        }
      } catch (error) {
        if (closed || (error as Error).name === "AbortError") return;
      }
      onStatus?.(false);
      if (closed) return;
      attempt += 1;
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * 2 ** attempt, 15000)));
    }
  };

  void run();
  return {
    close: () => {
      closed = true;
      controller.abort();
    },
  };
}
