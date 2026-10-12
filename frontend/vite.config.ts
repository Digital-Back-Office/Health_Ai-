import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs on :8000 in development; proxy it so the browser uses one origin.
// 127.0.0.1 rather than "localhost": on Windows "localhost" tries IPv6 first, while
// uvicorn listens on IPv4 only, which adds a delay to every proxied request.
const API = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API, changeOrigin: true },
      "/healthz": { target: API, changeOrigin: true },
    },
  },
  // The ElevenLabs SDK bundles a WebRTC client (LiveKit), so the main chunk is large by design.
  build: { sourcemap: true, chunkSizeWarningLimit: 1200 },
});
