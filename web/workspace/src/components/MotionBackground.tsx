import { useEffect, useState } from "react";
import "./MotionBackground.css";

/** MotionBackground (PR #6, Worker 9): the air behind the
 * glass. Four very large, very pale radial-gradient shapes
 * drift slowly (65–90s loops, transform-only, `alternate`
 * so each loop returns to its start). Whisper tints only —
 * pale blue, peach, mint — at 0.60–0.70 opacity over the
 * near-white wash (raised by Worker 11 so the motion reads
 * in a screenshot, still calm), so white glass panels over
 * them stay high-contrast. Nothing saturated, nothing purple, no hard
 * edges (heavy blur + gradients that fade to transparent).
 *
 * Cost guards: no animation work at all on phones (≤640px
 * renders nothing — the static wash remains); reduced
 * motion renders the same composition static (CSS);
 * a hidden tab pauses the animations (visibilitychange).
 * The coordinator mounts this as the first child of .app;
 * it is position: fixed, z-index 0, pointer-events none. */

const SHAPES = ["blue", "peach", "mint", "sky"] as const;

export function MotionBackground() {
  const [narrow, setNarrow] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(max-width: 640px)").matches,
  );
  const [hidden, setHidden] = useState(
    () => typeof document !== "undefined" && document.hidden,
  );

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 640px)");
    const onWidth = (e: MediaQueryListEvent) => setNarrow(e.matches);
    mq.addEventListener("change", onWidth);
    const onVis = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      mq.removeEventListener("change", onWidth);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  // Phones get the static wash only — no animation work.
  if (narrow) return null;

  return (
    <div
      className={hidden ? "motion-bg is-paused" : "motion-bg"}
      aria-hidden="true"
    >
      {SHAPES.map((name) => (
        <div key={name} className={`motion-bg-shape motion-bg-${name}`} />
      ))}
    </div>
  );
}
