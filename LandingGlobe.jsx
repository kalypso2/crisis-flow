import Globe from "react-globe.gl";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const EARTH_MAP =
  "https://unpkg.com/three-globe/example/img/earth-blue-marble.jpg";
const EARTH_BUMP =
  "https://unpkg.com/three-globe/example/img/earth-topology.png";

function easeOutCubic(t) {
  return 1 - (1 - t) ** 3;
}

/**
 * Compact WebGL Earth: gentle intro scale-up, then steady auto-spin (no pointer control).
 */
export default function LandingGlobe({ growGlobe, expandMs }) {
  const wrapRef = useRef(null);
  const globeRef = useRef(null);
  const dimsRef = useRef({ w: 200, h: 200 });
  const [dims, setDims] = useState({ w: 200, h: 200 });
  const [stageMin, setStageMin] = useState(400);
  const expandRaf = useRef(null);

  useEffect(() => {
    dimsRef.current = dims;
  }, [dims]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      const m = Math.floor(Math.min(r.width, r.height));
      setStageMin(Math.max(240, m));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (growGlobe) return;
    const small = Math.max(160, Math.floor(stageMin * 0.36));
    setDims({ w: small, h: small });
  }, [stageMin, growGlobe]);

  useLayoutEffect(() => {
    if (expandRaf.current) {
      cancelAnimationFrame(expandRaf.current);
      expandRaf.current = null;
    }
    if (!growGlobe) return undefined;

    const el = wrapRef.current;
    if (!el) return undefined;
    const r = el.getBoundingClientRect();
    const m = Math.max(240, Math.min(r.width, r.height));
    /* Final globe stays modest: ~58% of stage, hard cap for large viewports */
    const target = Math.min(Math.floor(m * 0.58), 420);
    const start = Math.max(
      160,
      Math.min(dimsRef.current.w, Math.floor(m * 0.36)),
    );
    const t0 = performance.now();
    let lastSet = 0;

    const tick = (now) => {
      const raw = Math.min(1, (now - t0) / expandMs);
      const e = easeOutCubic(raw);
      const s = Math.round(start + (target - start) * e);
      const throttle = raw >= 1 || now - lastSet >= 36;
      if (throttle) {
        lastSet = now;
        setDims({ w: s, h: s });
      }
      if (raw < 1) {
        expandRaf.current = requestAnimationFrame(tick);
      } else {
        expandRaf.current = null;
        setDims({ w: target, h: target });
      }
    };
    expandRaf.current = requestAnimationFrame(tick);

    return () => {
      if (expandRaf.current) cancelAnimationFrame(expandRaf.current);
      expandRaf.current = null;
    };
  }, [growGlobe, expandMs]);

  const onGlobeReady = useCallback(() => {
    const g = globeRef.current;
    if (!g) return;
    const c = g.controls();
    c.enableZoom = false;
    c.enablePan = false;
    c.enableRotate = false;
    c.rotateSpeed = 0.5;
    if (typeof c.enableDamping === "boolean") c.enableDamping = false;
    c.autoRotate = true;
    c.autoRotateSpeed = 2.85;
    g.pointOfView({ lat: 20, lng: -50, altitude: 2.35 }, 0);
  }, []);

  return (
    <div ref={wrapRef} className="landing__globeFill">
      <Globe
        ref={globeRef}
        width={dims.w}
        height={dims.h}
        backgroundColor="rgba(0,0,0,0)"
        globeImageUrl={EARTH_MAP}
        bumpImageUrl={EARTH_BUMP}
        showAtmosphere
        atmosphereColor="rgba(0, 229, 255, 0.45)"
        atmosphereAltitude={0.18}
        onGlobeReady={onGlobeReady}
      />
    </div>
  );
}
