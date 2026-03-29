import { useEffect, useLayoutEffect, useRef, useState } from "react";
import LandingGlobe from "./LandingGlobe.jsx";
import "./LandingPage.css";

const STORAGE_KEY = "crisisflow_dashboard_entered";

/** ms — globe scale-up duration (see LandingPage.css --globe-expand-duration) */
const EXPAND_MS = 3200;

export function hasLandingBeenDismissed() {
  try {
    return sessionStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function markLandingDismissed() {
  try {
    sessionStorage.setItem(STORAGE_KEY, "1");
  } catch {
    /* ignore */
  }
}

/**
 * Parallax + cursor glow: write CSS vars on the root synchronously (no setState),
 * using pointerrawupdate when available for minimal latency.
 */
function attachLandingPointerTracking(root) {
  const update = (e) => {
    const r = root.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    const cx = Math.min(1, Math.max(0, x));
    const cy = Math.min(1, Math.max(0, y));
    const nx = (cx - 0.5) * 2;
    const ny = (cy - 0.5) * 2;
    root.style.setProperty("--landing-nx", String(nx));
    root.style.setProperty("--landing-ny", String(ny));
    root.style.setProperty("--landing-mx", `${cx * 100}%`);
    root.style.setProperty("--landing-my", `${cy * 100}%`);
  };

  root.style.setProperty("--landing-nx", "0");
  root.style.setProperty("--landing-ny", "0");
  root.style.setProperty("--landing-mx", "50%");
  root.style.setProperty("--landing-my", "50%");

  const useRaw =
    typeof window !== "undefined" && "onpointerrawupdate" in window;
  const type = useRaw ? "pointerrawupdate" : "pointermove";
  root.addEventListener(type, update, { passive: true });
  return () => root.removeEventListener(type, update);
}

/**
 * Landing: smaller 3D globe with continuous auto-rotation; background tracks pointer in real time.
 */
export default function LandingPage({ onEnter }) {
  const rootRef = useRef(null);
  const [growGlobe, setGrowGlobe] = useState(false);

  useEffect(() => {
    const growId = requestAnimationFrame(() => setGrowGlobe(true));
    return () => cancelAnimationFrame(growId);
  }, []);

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    return attachLandingPointerTracking(root);
  }, []);

  const handleEnter = () => {
    markLandingDismissed();
    onEnter?.();
  };

  return (
    <div ref={rootRef} className="landing" role="presentation">
      <div className="landing__aurora" aria-hidden />
      <div className="landing__grid" aria-hidden />
      <div className="landing__cursorGlow" aria-hidden />

      <div className="landing__inner">
        <div
          className={`landing__globeStage ${growGlobe ? "landing__globeStage--grown" : ""}`}
        >
          <LandingGlobe growGlobe={growGlobe} expandMs={EXPAND_MS} />
        </div>

        <div className="landing__copyStack">
          <div className="landing__branding">
            <h1 className="landing__wordmark">
              <span className="landing__wordmark-crisis">Crisis</span>
              <span className="landing__wordmark-flow">Flow</span>
            </h1>
            <p className="landing__wordmark-sub">
              Global autonomous disaster intelligence
            </p>
          </div>

          <div className="landing__ctaBlock">
            <p className="landing__tagline">Join us in saving the world.</p>
            <button type="button" className="landing__enter" onClick={handleEnter}>
              Enter
            </button>
            <p className="landing__hint">Autonomous response dashboard</p>
          </div>
        </div>
      </div>
    </div>
  );
}
