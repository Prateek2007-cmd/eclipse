import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import "./EclipseHero.css";

/** EclipseHero (PR #6, Worker 9): the one 3D moment on the
 * starting page — an eclipse in miniature, above the
 * greeting in the Agent chat's empty state. The 3D scene
 * is lazy-loaded exactly like BoundaryScene (React.lazy +
 * Suspense), so three.js never blocks first paint; the
 * Suspense fallback is the static SVG, never a blank box.
 *
 * Fallbacks — every one renders the same static inline SVG
 * (ring + disc, same pale palette), so the hero is always
 * complete:
 * - no WebGL → SVG (checked before the scene mounts)
 * - prefers-reduced-motion → SVG (no animation)
 * - ≤640px width → SVG (no WebGL cost on phones)
 * - the 3D chunk fails to load, or the Canvas throws →
 *   the error boundary below catches it and renders the SVG
 * - tab hidden → the scene's demand loop pauses (see
 *   EclipseScene); the hero itself stays mounted.
 * The mode re-decides live if the media queries change. */

const EclipseScene = lazy(() => import("./EclipseScene"));

function webglAvailable(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return !!(
      canvas.getContext("webgl2") ||
      canvas.getContext("webgl") ||
      canvas.getContext("experimental-webgl")
    );
  } catch {
    return false;
  }
}

type HeroMode = "scene" | "svg";

function decideMode(): HeroMode {
  if (typeof window === "undefined") return "svg";
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches)
    return "svg";
  if (window.matchMedia("(max-width: 640px)").matches) return "svg";
  if (!webglAvailable()) return "svg";
  return "scene";
}

/** The static eclipse: halo, pale disc, tilted ring — the
 * same motif as the 3D scene, drawn inline so it costs
 * nothing and can never fail to load. */
function EclipseSvg() {
  return (
    <svg viewBox="0 0 160 160" role="presentation" aria-hidden="true">
      <ellipse
        cx="80"
        cy="80"
        rx="72"
        ry="24"
        fill="none"
        stroke="#DCE9F5"
        strokeWidth="2"
        transform="rotate(-18 80 80)"
      />
      <circle cx="80" cy="80" r="42" fill="#F8F1E4" />
      <circle cx="66" cy="64" r="10" fill="#FFFFFF" opacity="0.55" />
      <ellipse
        cx="80"
        cy="80"
        rx="62"
        ry="20"
        fill="none"
        stroke="#A9C6E2"
        strokeWidth="5"
        transform="rotate(-18 80 80)"
      />
    </svg>
  );
}

/** The hero must never throw if Three.js fails to load: a
 * rejected lazy chunk or a Canvas error lands here, and the
 * static SVG renders instead. Nothing propagates to the chat. */
class SceneErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }
  render(): ReactNode {
    if (this.state.failed) return <EclipseSvg />;
    return this.props.children;
  }
}

export function EclipseHero() {
  const [mode, setMode] = useState<HeroMode>(() => decideMode());

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const narrow = window.matchMedia("(max-width: 640px)");
    const onChange = () => setMode(decideMode());
    reduced.addEventListener("change", onChange);
    narrow.addEventListener("change", onChange);
    return () => {
      reduced.removeEventListener("change", onChange);
      narrow.removeEventListener("change", onChange);
    };
  }, []);

  if (mode === "svg") {
    return (
      <div
        className="eclipse-hero"
        role="img"
        aria-label="Eclipse — a pale disc with a thin ring around it"
      >
        <EclipseSvg />
      </div>
    );
  }

  return (
    <div
      className="eclipse-hero"
      role="img"
      aria-label="Eclipse — a pale disc with a thin ring around it, turning slowly"
    >
      <SceneErrorBoundary>
        <Suspense fallback={<EclipseSvg />}>
          <EclipseScene />
        </Suspense>
      </SceneErrorBoundary>
    </div>
  );
}
