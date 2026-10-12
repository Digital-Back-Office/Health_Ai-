import { useEffect, useRef } from "react";

export type OrbState = "idle" | "connecting" | "listening" | "speaking" | "muted" | "text";

interface Props {
  state: OrbState;
  /** Returns the current audio level (0-1). Read every animation frame, not via React state. */
  getLevel?: () => number;
}

/**
 * The assistant's "AI orb": a deep-teal sphere with slowly swirling light,
 * which pulses with the voice (the patient's mic while listening, Sophie's audio while speaking).
 */
export function VoiceOrb({ state, getLevel }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (!getLevel || state === "muted" || state === "text" || state === "idle" || state === "connecting") {
      element.style.setProperty("--level", "0");
      return;
    }
    let frame = 0;
    let smoothed = 0;
    const tick = () => {
      let raw = 0;
      try {
        raw = getLevel() || 0;
      } catch {
        raw = 0;
      }
      smoothed = smoothed * 0.72 + Math.min(1, raw * 1.8) * 0.28;
      element.style.setProperty("--level", smoothed.toFixed(3));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [getLevel, state]);

  return (
    <div ref={ref} className={`orb orb--${state}`} aria-hidden="true">
      <span className="orb__halo" />
      <span className="orb__track" />
      <span className="orb__sphere">
        <span className="orb__swirl orb__swirl--a" />
        <span className="orb__swirl orb__swirl--b" />
        <span className="orb__swirl orb__swirl--c" />
        <span className="orb__swirl orb__swirl--d" />
        <span className="orb__gloss" />
      </span>
    </div>
  );
}
