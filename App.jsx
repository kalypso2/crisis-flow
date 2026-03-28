import { useState, useEffect, useMemo } from "react";

/** Same host as the page + port 8000 so LAN / 127.0.0.1 / localhost all match the API. */
function apiOrigin() {
  const fromEnv = import.meta.env.VITE_API_ORIGIN;
  if (fromEnv) return fromEnv.replace(/\/$/, "");
  if (typeof window !== "undefined") {
    const { protocol, hostname } = window.location;
    return `${protocol}//${hostname}:8000`;
  }
  return "http://127.0.0.1:8000";
}

const SEV_COLOR = {
  1: "#378ADD", 2: "#1D9E75", 3: "#EF9F27", 4: "#D85A30", 5: "#E24B4A",
};
const SEV_LABEL = {
  1: "Low", 2: "Moderate", 3: "High", 4: "Critical", 5: "Extreme",
};
const TYPE_EMOJI = {
  earthquake: "🌍", flood: "🌊", cyclone: "🌀", volcano: "🌋",
  wildfire: "🔥", drought: "☀️", storm: "⛈️", conflict: "⚠️", iceberg: "🧊",
};

// ── Severity badge ────────────────────────────────────────────────────────
function SevBadge({ sev }) {
  return (
    <span style={{
      background: SEV_COLOR[sev] + "22",
      color: SEV_COLOR[sev],
      border: `1px solid ${SEV_COLOR[sev]}55`,
      borderRadius: 4,
      padding: "1px 7px",
      fontSize: 11,
      fontWeight: 500,
      whiteSpace: "nowrap",
    }}>
      {SEV_LABEL[sev] || "?"} {sev}
    </span>
  );
}

// ── Flag badge ────────────────────────────────────────────────────────────
function FlagBadge({ flag }) {
  if (!flag) return null;
  const color = flag === "FALLBACK_USED" ? "#D85A30" : "#EF9F27";
  return (
    <span style={{
      background: color + "18",
      color,
      border: `1px solid ${color}44`,
      borderRadius: 4,
      padding: "1px 6px",
      fontSize: 10,
      marginLeft: 6,
    }}>
      {flag}
    </span>
  );
}

// ── Single event card ─────────────────────────────────────────────────────
function EventCard({ ev, selected, onClick }) {
  return (
    <div
      onClick={onClick}
      style={{
        borderBottom: "0.5px solid var(--color-border-tertiary)",
        padding: "10px 14px",
        cursor: "pointer",
        background: selected ? "var(--color-background-secondary)" : "transparent",
        transition: "background 0.15s",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <span style={{ fontSize: 14 }}>{TYPE_EMOJI[ev.type] || "📍"}</span>
        <span style={{ fontSize: 12, fontWeight: 500, flex: 1, color: "var(--color-text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {ev.title}
        </span>
        <SevBadge sev={ev.severity} />
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
          {ev.source?.toUpperCase()}
        </span>
        <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>·</span>
        <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
          {ev.lat?.toFixed(2)}, {ev.lon?.toFixed(2)}
        </span>
        <FlagBadge flag={ev.consensus_flag} />
      </div>
    </div>
  );
}

// ── Detail panel ──────────────────────────────────────────────────────────
function DetailPanel({ ev, onClose }) {
  if (!ev) return (
    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--color-text-tertiary)", fontSize: 13 }}>
      Select an event to see details
    </div>
  );

  const alloc = ev.allocation || {};
  return (
    <div style={{ flex: 1, padding: 20, overflowY: "auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 500, color: "var(--color-text-primary)", marginBottom: 4 }}>
            {TYPE_EMOJI[ev.type]} {ev.title}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <SevBadge sev={ev.severity} />
            <span style={{ fontSize: 11, color: "var(--color-text-tertiary)", textTransform: "uppercase" }}>{ev.type}</span>
            <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>via {ev.source?.toUpperCase()}</span>
            <FlagBadge flag={ev.consensus_flag} />
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--color-text-tertiary)", padding: 0 }}>×</button>
      </div>

      <Section title="Action summary">
        <p style={{ fontSize: 13, lineHeight: 1.6, color: "var(--color-text-secondary)", margin: 0 }}>
          {ev.action_summary || "—"}
        </p>
      </Section>

      <Section title="Location">
        <Row label="Lat / Lon">{ev.lat?.toFixed(4)}, {ev.lon?.toFixed(4)}</Row>
        <Row label="Radius">{ev.radius_km} km</Row>
        <Row label="Affected pop.">{ev.affected_population?.toLocaleString() || "unknown"}</Row>
        {ev.location_name && <Row label="Area">{ev.location_name}</Row>}
      </Section>

      {alloc.depot_name && (
        <Section title="Resource allocation">
          <Row label="Depot">{alloc.depot_name}</Row>
          <Row label="ETA">{alloc.eta_minutes} min</Row>
          <Row label="Resources">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 2 }}>
              {(alloc.resources || []).map(r => (
                <span key={r} style={{ fontSize: 11, background: "var(--color-background-secondary)", border: "0.5px solid var(--color-border-secondary)", borderRadius: 4, padding: "2px 7px", color: "var(--color-text-secondary)" }}>{r}</span>
              ))}
            </div>
          </Row>
        </Section>
      )}

      {ev.domain_tags?.length > 0 && (
        <Section title="Domain tags">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {ev.domain_tags.map(t => (
              <span key={t} style={{ fontSize: 11, color: "var(--color-text-info)", background: "var(--color-background-info)", borderRadius: 4, padding: "2px 7px" }}>{t}</span>
            ))}
          </div>
        </Section>
      )}

      <Section title="Meta">
        <Row label="Event ID"><span style={{ fontFamily: "monospace", fontSize: 11 }}>{ev.id?.slice(0, 16)}…</span></Row>
        <Row label="Timestamp">{new Date(ev.timestamp).toLocaleString()}</Row>
        <Row label="Status">{ev.status}</Row>
        <Row label="Confidence">{(ev.confidence * 100).toFixed(0)}%</Row>
      </Section>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase", marginBottom: 8 }}>{title}</div>
      <div style={{ background: "var(--color-background-secondary)", borderRadius: 8, padding: "10px 12px" }}>
        {children}
      </div>
    </div>
  );
}

function Row({ label, children }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "3px 0", borderBottom: "0.5px solid var(--color-border-tertiary)", color: "var(--color-text-secondary)" }}>
      <span style={{ color: "var(--color-text-tertiary)" }}>{label}</span>
      <span style={{ textAlign: "right", maxWidth: "65%" }}>{children}</span>
    </div>
  );
}

// ── Agent health panel ────────────────────────────────────────────────────
function HealthPanel({ health }) {
  if (!health) return null;
  return (
    <div style={{ borderTop: "0.5px solid var(--color-border-tertiary)", padding: "10px 14px" }}>
      <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", marginBottom: 8 }}>AGENT STATUS</div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {Object.entries(health.agents || {}).map(([name, info]) => (
          <div key={name} style={{
            fontSize: 10, borderRadius: 4, padding: "2px 8px",
            background: info.status === "OK" ? "var(--color-background-success)" : "var(--color-background-danger)",
            color: info.status === "OK" ? "var(--color-text-success)" : "var(--color-text-danger)",
            border: `0.5px solid ${info.status === "OK" ? "var(--color-border-success)" : "var(--color-border-danger)"}`,
          }}>
            {name} {info.status === "DEGRADED" ? `⚠ ${info.recent_failures}f` : "✓"}
          </div>
        ))}
      </div>
      <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", marginTop: 6 }}>
        Queue: {health.queue_depth} · Processed: {health.processed_total}
      </div>
    </div>
  );
}

// ── Globe placeholder ─────────────────────────────────────────────────────
function GlobePlaceholder({ events }) {
  return (
    <div style={{
      flex: 1,
      background: "var(--color-background-secondary)",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      position: "relative",
      overflow: "hidden",
    }}>
      <div style={{ position: "absolute", top: 16, left: 20, fontSize: 11, color: "var(--color-text-tertiary)" }}>
        3D globe — mount Globe.gl here (see README)
      </div>

      {/* Active event dots grid */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, maxWidth: 400, justifyContent: "center" }}>
        {events.slice(-24).map(ev => (
          <div key={ev.id} title={ev.title} style={{
            width: 10 + ev.severity * 3,
            height: 10 + ev.severity * 3,
            borderRadius: "50%",
            background: SEV_COLOR[ev.severity] || "#888",
            opacity: 0.8,
            border: ev.consensus_flag ? "2px solid #EF9F27" : "none",
          }} />
        ))}
        {events.length === 0 && (
          <div style={{ color: "var(--color-text-tertiary)", fontSize: 13 }}>
            Waiting for events…
          </div>
        )}
      </div>

      <div style={{ position: "absolute", bottom: 16, left: 20, display: "flex", gap: 14 }}>
        {[1,2,3,4,5].map(s => (
          <div key={s} style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <div style={{ width: 10, height: 10, borderRadius: "50%", background: SEV_COLOR[s] }} />
            <span style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>{SEV_LABEL[s]}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Stats bar ─────────────────────────────────────────────────────────────
function StatsBar({ events }) {
  const counts = events.reduce((acc, e) => {
    acc[e.type] = (acc[e.type] || 0) + 1;
    return acc;
  }, {});
  const high = events.filter(e => e.severity >= 4).length;
  const flagged = events.filter(e => e.consensus_flag).length;

  return (
    <div style={{
      display: "flex", gap: 0,
      borderBottom: "0.5px solid var(--color-border-tertiary)",
      overflowX: "auto",
    }}>
      {[
        ["Total", events.length, "var(--color-text-primary)"],
        ["Critical+", high, "#D85A30"],
        ["Flagged", flagged, "#EF9F27"],
        ...Object.entries(counts).slice(0, 5).map(([t, c]) => [
          TYPE_EMOJI[t] + " " + t, c, "var(--color-text-secondary)"
        ]),
      ].map(([label, val, color]) => (
        <div key={label} style={{
          padding: "8px 16px",
          borderRight: "0.5px solid var(--color-border-tertiary)",
          minWidth: 70,
          textAlign: "center",
        }}>
          <div style={{ fontSize: 16, fontWeight: 500, color }}>{val}</div>
          <div style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>{label}</div>
        </div>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Main App
// ═══════════════════════════════════════════════════════════════════════════

export default function App() {
  const [events, setEvents] = useState([]);
  const [selected, setSelected] = useState(null);
  const [health, setHealth] = useState(null);
  const [connected, setConnected] = useState(false);
  const [filter, setFilter] = useState("all");

  const base = useMemo(() => apiOrigin(), []);
  const sseUrl = `${base}/stream`;
  const healthUrl = `${base}/health`;

  // SSE connection
  useEffect(() => {
    const es = new EventSource(sseUrl);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data);
        if (!ev || typeof ev.id !== "string") return;
        setEvents((prev) => {
          const without = prev.filter((x) => x.id !== ev.id);
          return [ev, ...without].slice(0, 100);
        });
      } catch {
        /* ignore malformed SSE payloads */
      }
    };
    return () => es.close();
  }, [sseUrl]);

  // Health polling
  useEffect(() => {
    const poll = () =>
      fetch(healthUrl).then((r) => r.json()).then(setHealth).catch(() => {});
    poll();
    const id = setInterval(poll, 10000);
    return () => clearInterval(id);
  }, [healthUrl]);

  const filtered = filter === "all"
    ? events
    : filter === "conflict"
    ? events.filter(e => e.type === "conflict")
    : filter === "critical"
    ? events.filter(e => e.severity >= 4)
    : filter === "flagged"
    ? events.filter(e => e.consensus_flag)
    : events.filter(e => e.type === filter);

  return (
    <div style={{
      display: "flex", flexDirection: "column", height: "100vh",
      fontFamily: "var(--font-sans)", color: "var(--color-text-primary)",
    }}>
      {/* Header */}
      <div style={{
        display: "flex", alignItems: "center", gap: 12,
        padding: "10px 18px",
        borderBottom: "0.5px solid var(--color-border-tertiary)",
      }}>
        <span style={{ fontSize: 16, fontWeight: 500 }}>CrisisFlow</span>
        <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
          Autonomous disaster response system
        </span>
        <div style={{ flex: 1 }} />
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{
            width: 7, height: 7, borderRadius: "50%",
            background: connected ? "#1D9E75" : "#E24B4A",
          }} />
          <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
            {connected ? "live" : "connecting…"}
          </span>
        </div>
      </div>

      <StatsBar events={events} />

      {/* Filter bar */}
      <div style={{
        display: "flex", gap: 0, padding: "6px 14px",
        borderBottom: "0.5px solid var(--color-border-tertiary)",
        overflowX: "auto",
      }}>
        {["all","critical","conflict","flagged","earthquake","flood","storm","wildfire","cyclone"].map(f => (
          <button key={f} onClick={() => setFilter(f)} style={{
            padding: "4px 12px", fontSize: 11, border: "none", cursor: "pointer",
            borderRadius: 4, marginRight: 4,
            background: filter === f ? "var(--color-background-info)" : "transparent",
            color: filter === f ? "var(--color-text-info)" : "var(--color-text-tertiary)",
            fontWeight: filter === f ? 500 : 400,
          }}>
            {f}
          </button>
        ))}
      </div>

      {/* Main layout */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        {/* Event list */}
        <div style={{
          width: 320, borderRight: "0.5px solid var(--color-border-tertiary)",
          display: "flex", flexDirection: "column", overflow: "hidden",
        }}>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {filtered.length === 0 && (
              <div style={{ padding: 20, fontSize: 13, color: "var(--color-text-tertiary)", textAlign: "center" }}>
                No events yet
              </div>
            )}
            {filtered.map(ev => (
              <EventCard
                key={ev.id}
                ev={ev}
                selected={selected?.id === ev.id}
                onClick={() => setSelected(ev)}
              />
            ))}
          </div>
          <HealthPanel health={health} />
        </div>

        {/* Globe + detail */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
          <GlobePlaceholder events={filtered} />
        </div>

        {/* Detail panel */}
        {selected && (
          <div style={{
            width: 340, borderLeft: "0.5px solid var(--color-border-tertiary)",
            overflowY: "auto", display: "flex", flexDirection: "column",
          }}>
            <DetailPanel ev={selected} onClose={() => setSelected(null)} />
          </div>
        )}
      </div>
    </div>
  );
}
