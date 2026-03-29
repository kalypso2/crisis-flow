import { useState, useEffect, useMemo, useRef } from "react";
import Globe from "react-globe.gl";

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
const TYPE_COLOR = {
  earthquake: "#A0522D",
  flood:      "#2196F3",
  cyclone:    "#9C27B0",
  volcano:    "#FF5722",
  wildfire:   "#FF9800",
  drought:    "#D4A017",
  storm:      "#78909C",
  conflict:   "#E24B4A",
  iceberg:    "#00BCD4",
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
  if (!flag || flag === "LOW_CONFIDENCE") return null;
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

// Resource keys aligned with distribution_engine / need_calculator
const AID_RESOURCE_KEYS = ["shelter_kits", "food_rations", "medical_kits", "water_kits", "vehicles"];

/**
 * Aid button colour from week simulation: average fill across all five stock types.
 * Categories with no need count as 100% for the average.
 * Bands on average: 100% green, 51–99% yellow, 26–50% orange, 0–25% red.
 */
function getAidButtonStyle(ev, distResult) {
  const neutral = {
    border: "1px solid var(--color-border-secondary)",
    background: "var(--color-background-secondary)",
    color: "var(--color-text-tertiary)",
  };
  if (!distResult || !ev?.id) return { style: neutral, avgPct: null };
  const needMap = distResult.need || {};
  const committedMap = distResult.committed || {};
  const need = needMap[ev.id];
  const committed = committedMap[ev.id];
  if (!need || !committed) return { style: neutral, avgPct: null };

  let sum = 0;
  for (const k of AID_RESOURCE_KEYS) {
    const n = need[k] ?? 0;
    if (n <= 0) {
      sum += 1;
      continue;
    }
    const c = Math.min(n, Math.max(0, committed[k] ?? 0));
    sum += c / n;
  }
  const avgF = sum / AID_RESOURCE_KEYS.length;
  const avgPct = Math.round(avgF * 100);

  if (avgF >= 1) {
    return {
      style: {
        border: "1px solid #1D9E7588",
        background: "#1D9E7514",
        color: "#1D9E75",
      },
      avgPct,
    };
  }
  if (avgF >= 0.51) {
    return {
      style: {
        border: "1px solid #C9A82099",
        background: "#F4E04D28",
        color: "#A67C00",
      },
      avgPct,
    };
  }
  if (avgF >= 0.26) {
    return {
      style: {
        border: "1px solid #D85A3099",
        background: "#D85A3018",
        color: "#D85A30",
      },
      avgPct,
    };
  }
  return {
    style: {
      border: "1px solid #E24B4A99",
      background: "#E24B4A18",
      color: "#E24B4A",
    },
    avgPct,
  };
}

// ── Single event card ─────────────────────────────────────────────────────
function EventCard({ ev, selected, onClick, onAidClick, distResult }) {
  const { style: aidBtnStyle, avgPct } = getAidButtonStyle(ev, distResult);
  const aidTitle = avgPct == null
    ? "View aid requirements (run week simulation for fill status)"
    : `View aid requirements — average supply fill: ${avgPct}%`;

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
        <button
          onClick={e => { e.stopPropagation(); onAidClick?.(ev); }}
          title={aidTitle}
          style={{
            fontSize: 10, fontWeight: 600,
            padding: "2px 7px", borderRadius: 4,
            cursor: "pointer",
            whiteSpace: "nowrap",
            lineHeight: 1.4,
            ...aidBtnStyle,
          }}
        >
          Aid
        </button>
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

// ── Aid requirement panel ─────────────────────────────────────────────────
const AID_TYPE_LABEL = {
  shelter_kits:  { label: "Shelter Kits",   unit: "kits",        icon: "🏕️", note: "1 kit = 1 family of 5 for 30 days" },
  food_rations:  { label: "Food Rations",   unit: "person-days", icon: "🍱", note: "2,100 kcal / person / day" },
  medical_kits:  { label: "Medical Kits",   unit: "IEHK units",  icon: "🩺", note: "1 kit covers 10,000 people / 3 months" },
  water_kits:    { label: "Water Kits",     unit: "units",       icon: "💧", note: "15 L / person / day for a family of 5" },
  vehicles:      { label: "Field Vehicles", unit: "vehicles",    icon: "🚛", note: "Light 4×4 for distribution & access" },
};

// Colour of progress bar: green when full, amber when partial, red when none
function _barColor(pct) {
  if (pct >= 1)    return "#1D9E75";
  if (pct >= 0.5)  return "#EF9F27";
  if (pct > 0)     return "#D85A30";
  return "#E24B4A";
}

function AidPanel({ ev, distResult, onClose }) {
  if (!ev) return null;
  const alloc     = ev.allocation || {};
  const need      = alloc.need    || {};
  const resources = alloc.resources || [];

  // Pull committed quantities from the distribution simulation
  const committed  = (distResult?.committed  || {})[ev.id] || {};
  const needMap    = (distResult?.need       || {})[ev.id] || {};

  // Feed entries for this specific event
  const evFeed = (distResult?.feed || []).filter(f => f.event_id === ev.id);

  const hasSphereData = (need.displaced > 0) || Object.keys(needMap).length > 0;

  // Use simulation need if available (more accurate), fall back to pipeline need
  const needQty  = (key) => needMap[key]  ?? need[key]  ?? 0;
  const commQty  = (key) => committed[key] ?? 0;

  return (
    <div style={{ width: 360, borderLeft: "0.5px solid var(--color-border-tertiary)", display: "flex", flexDirection: "column", overflowY: "auto" }}>
      {/* Header */}
      <div style={{ padding: "16px 20px 12px", borderBottom: "0.5px solid var(--color-border-tertiary)", display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexShrink: 0 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#1D9E75", marginBottom: 2 }}>Aid Requirements</div>
          <div style={{ fontSize: 11, color: "var(--color-text-tertiary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 280 }}>
            {TYPE_EMOJI[ev.type]} {ev.title}
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--color-text-tertiary)", padding: 0 }}>×</button>
      </div>

      <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>

        {/* Context row */}
        <div style={{ background: "var(--color-background-secondary)", borderRadius: 8, padding: "10px 14px", display: "flex", gap: 10 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", marginBottom: 3 }}>Severity</div>
            <SevBadge sev={ev.severity} />
          </div>
          {(need.window_days || needQty("window_days")) > 0 && (
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", marginBottom: 3 }}>Response window</div>
              <div style={{ fontSize: 13, fontWeight: 500, color: "var(--color-text-primary)" }}>{need.window_days ?? needQty("window_days")} days</div>
            </div>
          )}
          {(need.displaced || 0) > 0 && (
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", marginBottom: 3 }}>Est. displaced</div>
              <div style={{ fontSize: 13, fontWeight: 500, color: "var(--color-text-primary)" }}>{(need.displaced || 0).toLocaleString()}</div>
            </div>
          )}
        </div>

        {/* A. Type of aid */}
        <div>
          <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase", marginBottom: 8 }}>
            A. Type of Aid Needed
          </div>
          <div style={{ background: "var(--color-background-secondary)", borderRadius: 8, padding: "10px 14px", display: "flex", flexWrap: "wrap", gap: 6 }}>
            {resources.length > 0
              ? resources.map(r => (
                  <span key={r} style={{ fontSize: 12, background: "#1D9E7514", border: "1px solid #1D9E7544", borderRadius: 4, padding: "3px 9px", color: "#1D9E75" }}>{r}</span>
                ))
              : <span style={{ fontSize: 12, color: "var(--color-text-tertiary)" }}>Assessment required</span>
            }
          </div>
        </div>

        {/* B. Quantities + progress bars */}
        <div>
          <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase", marginBottom: 8 }}>
            B. Quantities Required & Distributed
          </div>
          {hasSphereData ? (
            <div style={{ background: "var(--color-background-secondary)", borderRadius: 8, overflow: "hidden" }}>
              {Object.entries(AID_TYPE_LABEL).map(([key, meta], i, arr) => {
                const needed = needQty(key);
                const given  = commQty(key);
                if (needed === 0) return null;
                const pct = Math.min(1, needed > 0 ? given / needed : 0);
                const barColor = _barColor(pct);
                const isLast = i === arr.length - 1 || !Object.entries(AID_TYPE_LABEL).slice(i + 1).some(([k]) => needQty(k) > 0);
                return (
                  <div key={key} style={{
                    padding: "10px 14px",
                    borderBottom: isLast ? "none" : "0.5px solid var(--color-border-tertiary)",
                  }}>
                    {/* Label + amounts */}
                    <div style={{ display: "flex", alignItems: "baseline", gap: 6, marginBottom: 4 }}>
                      <span style={{ fontSize: 16, lineHeight: 1 }}>{meta.icon}</span>
                      <span style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)", flex: 1 }}>{meta.label}</span>
                      <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
                        <span style={{ fontWeight: 600, color: barColor }}>{given.toLocaleString()}</span>
                        {" / "}{needed.toLocaleString()}
                        {" "}<span style={{ fontSize: 10 }}>{meta.unit}</span>
                      </span>
                    </div>
                    {/* Progress bar */}
                    <div style={{ height: 5, borderRadius: 3, background: "var(--color-border-secondary)" }}>
                      <div style={{ height: 5, borderRadius: 3, background: barColor, width: `${Math.round(pct * 100)}%`, transition: "width 0.4s, background 0.4s" }} />
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginTop: 2 }}>
                      <span style={{ fontSize: 10, color: "var(--color-text-tertiary)", fontStyle: "italic" }}>{meta.note}</span>
                      <span style={{ fontSize: 10, fontWeight: 600, color: barColor }}>{Math.round(pct * 100)}%</span>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div style={{ background: "var(--color-background-secondary)", borderRadius: 8, padding: 14, fontSize: 12, color: "var(--color-text-tertiary)", textAlign: "center" }}>
              {distResult ? "Below deployment threshold (sev ≤ 1)" : "Run week simulation to see distribution"}
            </div>
          )}
        </div>

        {/* Per-event allocation feed */}
        {evFeed.length > 0 && (
          <div>
            <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase", marginBottom: 8 }}>
              Hub Contributions (priority #{evFeed[0]?.priority_rank})
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {evFeed.map((entry, i) => (
                <div key={i} style={{ background: "var(--color-background-secondary)", borderRadius: 8, padding: "10px 12px" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                    <span style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)" }}>
                      ✈️ {entry.hub_name}
                    </span>
                    {entry.needs_fully_met && i === evFeed.length - 1 && (
                      <span style={{ fontSize: 10, background: "#1D9E7520", color: "#1D9E75", borderRadius: 3, padding: "1px 5px", fontWeight: 600 }}>
                        FULLY MET
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", marginBottom: 6 }}>{entry.hub_org}</div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {Object.entries(entry.supplies).filter(([, v]) => v > 0).map(([k, v]) => (
                      <span key={k} style={{ fontSize: 10, background: "var(--color-background-primary)", border: "0.5px solid var(--color-border-secondary)", borderRadius: 3, padding: "2px 6px", color: "var(--color-text-secondary)" }}>
                        {AID_TYPE_LABEL[k]?.icon} {k.replace(/_/g, " ")}: <strong>{v.toLocaleString()}</strong>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {evFeed.length === 0 && distResult && hasSphereData && (
          <div style={{ fontSize: 11, color: "#D85A30", background: "#D85A3010", border: "1px solid #D85A3030", borderRadius: 6, padding: "8px 12px" }}>
            ⚠ No hubs had sufficient stock to serve this event this week.
          </div>
        )}

        {(need.notes || []).length > 0 && (
          <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", fontStyle: "italic", lineHeight: 1.5 }}>
            {need.notes.join(" · ")}
          </div>
        )}

        <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", borderTop: "0.5px solid var(--color-border-tertiary)", paddingTop: 10 }}>
          Based on Sphere Handbook 2018. Initial emergency phase only.
        </div>
      </div>
    </div>
  );
}

// ── Transport icon ────────────────────────────────────────────────────────
const TRANSPORT_ICON = { air: "✈️", land: "🚛", sea: "🚢" };

// ── Stock level colour ────────────────────────────────────────────────────
const STOCK_COLOR = { high: "#1D9E75", medium: "#EF9F27", low: "#D85A30", critical: "#E24B4A", unknown: "#888780" };

// ── Inventory bar — shown in the sidebar footer ───────────────────────────
function InventoryBar({ inventory }) {
  if (!inventory || inventory.length === 0) return null;
  return (
    <div style={{ borderTop: "0.5px solid var(--color-border-tertiary)", padding: "10px 14px" }}>
      <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", marginBottom: 8 }}>
        HUB INVENTORY
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {inventory.map(hub => {
          const pct = {
            high: 80, medium: 50, low: 20, critical: 5, unknown: 0,
          }[hub.stock_level] ?? 0;
          const color = STOCK_COLOR[hub.stock_level];
          return (
            <div key={hub.hub_name} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                  <span style={{ fontSize: 10, color: "var(--color-text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 140 }}>
                    {hub.hub_name.replace(" UNHRD", "").replace(" UNHCR", "").replace(" OCHA", "").replace(" WFP", "")}
                  </span>
                  <span style={{ fontSize: 10, color, fontWeight: 600 }}>
                    {hub.stock_level}
                  </span>
                </div>
                <div style={{ height: 4, borderRadius: 2, background: "var(--color-border-secondary)" }}>
                  <div style={{ height: 4, borderRadius: 2, background: color, width: `${pct}%`, transition: "width 0.5s" }} />
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Detail panel ──────────────────────────────────────────────────────────
function DetailPanel({ ev, binEvents, onSelectBinEvent, onClose }) {
  if (!ev) return (
    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--color-text-tertiary)", fontSize: 13 }}>
      Select an event to see details
    </div>
  );

  const alloc = ev.allocation || {};
  return (
    <div style={{ flex: 1, overflowY: "auto" }}>
      {/* Bin picker — shown only when a hex with multiple events is clicked */}
      {binEvents && binEvents.length > 1 && (
        <div style={{ borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
          <div style={{ padding: "8px 14px 4px", fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase" }}>
            {binEvents.length} events in this area
          </div>
          <div style={{ maxHeight: 160, overflowY: "auto" }}>
            {binEvents.map(binEv => (
              <div
                key={binEv.id}
                onClick={() => onSelectBinEvent(binEv)}
                style={{
                  display: "flex", alignItems: "center", gap: 8,
                  padding: "6px 14px", cursor: "pointer",
                  background: binEv.id === ev.id ? "var(--color-background-secondary)" : "transparent",
                  borderLeft: binEv.id === ev.id ? "2px solid var(--color-text-info)" : "2px solid transparent",
                  fontSize: 12,
                }}
              >
                <span>{TYPE_EMOJI[binEv.type] || "📍"}</span>
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--color-text-primary)" }}>
                  {binEv.title}
                </span>
                <SevBadge sev={binEv.severity} />
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ padding: 20 }}>
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
        <Section title="Aid deployment">
          <Row label="Lead hub">{alloc.depot_name}</Row>
          {alloc.depot_org && <Row label="Organisation">{alloc.depot_org}</Row>}
          <Row label="Transport">{TRANSPORT_ICON[alloc.transport_mode] || "✈️"} {alloc.transport_mode || "air"}</Row>
          <Row label="Lead ETA">{alloc.eta_minutes} min</Row>
          <Row label="Resources">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 2 }}>
              {(alloc.resources || []).map(r => (
                <span key={r} style={{ fontSize: 11, background: "var(--color-background-secondary)", border: "0.5px solid var(--color-border-secondary)", borderRadius: 4, padding: "2px 7px", color: "var(--color-text-secondary)" }}>{r}</span>
              ))}
            </div>
          </Row>
        </Section>
      )}

      {alloc.need && alloc.need.displaced > 0 && (
        <Section title="Aid need (Sphere standards)">
          <Row label="Est. displaced">{(alloc.need.displaced || 0).toLocaleString()}</Row>
          <Row label="Planning window">{alloc.need.window_days} days</Row>
          <Row label="Shelter kits">{(alloc.need.shelter_kits || 0).toLocaleString()}</Row>
          <Row label="Food rations">{(alloc.need.food_rations || 0).toLocaleString()}</Row>
          <Row label="Medical kits">{(alloc.need.medical_kits || 0).toLocaleString()}</Row>
          <Row label="Water kits">{(alloc.need.water_kits || 0).toLocaleString()}</Row>
          <Row label="Vehicles">{alloc.need.vehicles || 0}</Row>
          {(alloc.need.notes || []).length > 0 && (
            <div style={{ marginTop: 6, fontSize: 11, color: "var(--color-text-tertiary)", fontStyle: "italic" }}>
              {alloc.need.notes.join(" · ")}
            </div>
          )}
        </Section>
      )}

      {(alloc.convoys || []).length > 1 && (
        <Section title={`Convoys (${alloc.convoys.length} hubs)`}>
          {alloc.convoys.map((c, i) => (
            <div key={i} style={{ borderBottom: i < alloc.convoys.length - 1 ? "0.5px solid var(--color-border-tertiary)" : "none", paddingBottom: 8, marginBottom: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)" }}>
                  {TRANSPORT_ICON[c.transport] || "✈️"} {c.hub_name}
                </span>
                <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
                  {c.eta_minutes}m · {Math.round(c.dist_km)} km
                </span>
              </div>
              {c.hub_org && (
                <div style={{ fontSize: 11, color: "var(--color-text-tertiary)", marginBottom: 4 }}>{c.hub_org}</div>
              )}
              <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
                {Object.entries(c.supplies || {}).filter(([,v]) => v > 0).map(([k, v]) => (
                  <span key={k} style={{ fontSize: 10, background: "var(--color-background-secondary)", border: "0.5px solid var(--color-border-secondary)", borderRadius: 3, padding: "1px 5px", color: "var(--color-text-secondary)" }}>
                    {k.replace("_", " ")}: {v.toLocaleString()}
                  </span>
                ))}
              </div>
            </div>
          ))}
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
      </Section>
      </div>
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

// ── 3D globe ──────────────────────────────────────────────────────────────
function dominantType(hexBin) {
  const counts = {};
  hexBin.points.forEach(p => {
    counts[p.type] = (counts[p.type] || 0) + 1;
  });
  const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  return sorted.length > 0 ? sorted[0][0] : "unknown";
}

function GlobePanel({ events, onSelect }) {
  const globeRef = useRef(null);

  const validEvents = useMemo(() =>
    events.filter(ev => Number.isFinite(ev.lat) && Number.isFinite(ev.lon)),
  [events]);

  const arcsData = useMemo(() => {
    const result = [];
    for (const ev of events) {
      const dest = ev.arc_dest;
      if (!Array.isArray(dest) || dest.length < 2) continue;
      const color = TYPE_COLOR[ev.type] || "#888888";

      // Multi-hub: one arc per convoy leg
      if (Array.isArray(ev.arcs) && ev.arcs.length > 0) {
        for (const arc of ev.arcs) {
          if (arc.src_lat != null && arc.src_lon != null) {
            result.push({
              startLat: arc.src_lat,
              startLng: arc.src_lon,
              endLat:   dest[0],
              endLng:   dest[1],
              color,
              hubName:  arc.hub_name || "",
              transport: arc.transport || "air",
            });
          }
        }
      } else if (Array.isArray(ev.arc_source) && ev.arc_source.length >= 2) {
        result.push({
          startLat: ev.arc_source[0],
          startLng: ev.arc_source[1],
          endLat:   dest[0],
          endLng:   dest[1],
          color,
        });
      }
    }
    return result;
  }, [events]);

  useEffect(() => {
    if (!globeRef.current) return;
    globeRef.current.controls().autoRotate = true;
    globeRef.current.controls().autoRotateSpeed = 0.25;
  }, []);

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
        3D WebGL heatmap
      </div>

      <div style={{ position: "absolute", inset: 0 }}>
        <Globe
          ref={globeRef}
          width={window.innerWidth - 660}
          height={window.innerHeight - 220}
          backgroundColor="rgba(0,0,0,0)"
          globeImageUrl="//unpkg.com/three-globe/example/img/earth-blue-marble.jpg"
          bumpImageUrl="//unpkg.com/three-globe/example/img/earth-topology.png"
          hexBinPointsData={validEvents}
          hexBinPointLat="lat"
          hexBinPointLng={d => d.lon}
          hexBinPointWeight={d => d.severity || 1}
          hexBinResolution={4}
          hexTopColor={d => TYPE_COLOR[dominantType(d)] || "#888888"}
          hexSideColor={d => (TYPE_COLOR[dominantType(d)] || "#888888") + "99"}
          hexAltitude={d => d.sumWeight * 0.008}
          hexBinMerge={false}
          onHexClick={hex => onSelect?.(hex.points)}
          arcsData={arcsData}
          arcColor={a => a.color}
          arcStroke={0.6}
          arcDashLength={0.5}
          arcDashGap={1}
          arcDashAnimateTime={1800}
        />
      </div>

      {events.length === 0 && (
        <div style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          color: "var(--color-text-tertiary)",
          fontSize: 13,
          pointerEvents: "none",
        }}>
          Waiting for events...
        </div>
      )}

      <div style={{ position: "absolute", top: 16, right: 20, fontSize: 11, color: "var(--color-text-tertiary)" }}>
        Events: {validEvents.length} · Arcs: {arcsData.length}
      </div>

      <div style={{ position: "absolute", bottom: 16, left: 20, display: "flex", flexWrap: "wrap", gap: 10, maxWidth: "65%" }}>
        {Object.entries(TYPE_COLOR).map(([type, color]) => (
          <div key={type} style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <div style={{ width: 10, height: 10, borderRadius: "50%", background: color }} />
            <span style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>{type}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Stats bar (clickable filter tabs) ─────────────────────────────────────
function StatsBar({ events, filter, onFilter }) {
  const counts = events.reduce((acc, e) => {
    acc[e.type] = (acc[e.type] || 0) + 1;
    return acc;
  }, {});
  const high = events.filter(e => e.severity >= 4).length;
  const flagged = events.filter(e => e.consensus_flag).length;

  // Non-filterable summary cells (no onClick)
  const summaryItems = [
    { label: "Total",     val: events.length, color: "var(--color-text-primary)",   key: null },
    { label: "Critical+", val: high,          color: "#D85A30",                     key: null },
    { label: "Flagged",   val: flagged,        color: "#EF9F27",                     key: null },
  ];

  // Filterable type cells — "all" first, then each type that has events
  const typeItems = [
    { label: "all", val: events.length, color: "var(--color-text-secondary)", key: "all" },
    ...Object.entries(counts).map(([t, c]) => ({
      label: TYPE_EMOJI[t] ? `${TYPE_EMOJI[t]} ${t}` : t,
      val: c,
      color: TYPE_COLOR[t] || "var(--color-text-secondary)",
      key: t,
    })),
  ];

  const cellStyle = (active) => ({
    padding: "8px 16px",
    borderRight: "0.5px solid var(--color-border-tertiary)",
    minWidth: 70,
    textAlign: "center",
    background: active ? "var(--color-background-info)" : "transparent",
    transition: "background 0.15s",
  });

  return (
    <div style={{
      display: "flex", gap: 0,
      borderBottom: "0.5px solid var(--color-border-tertiary)",
      overflowX: "auto",
    }}>
      {summaryItems.map(({ label, val, color }) => (
        <div key={label} style={cellStyle(false)}>
          <div style={{ fontSize: 16, fontWeight: 500, color }}>{val}</div>
          <div style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>{label}</div>
        </div>
      ))}

      {/* Divider between summary and filter cells */}
      <div style={{ width: 1, background: "var(--color-border-secondary)", margin: "6px 0" }} />

      {typeItems.map(({ label, val, color, key }) => (
        <button
          key={key}
          onClick={() => onFilter(key)}
          style={{
            ...cellStyle(filter === key),
            border: "none",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          <div style={{
            fontSize: 16, fontWeight: 500,
            color: filter === key ? "var(--color-text-info)" : color,
          }}>{val}</div>
          <div style={{
            fontSize: 10,
            color: filter === key ? "var(--color-text-info)" : "var(--color-text-tertiary)",
            fontWeight: filter === key ? 600 : 400,
          }}>{label}</div>
        </button>
      ))}
    </div>
  );
}

// ── Global distribution feed ──────────────────────────────────────────────
const TYPE_LABEL = {
  earthquake: "Earthquake", flood: "Flood", cyclone: "Cyclone", volcano: "Volcano",
  wildfire: "Wildfire", drought: "Drought", storm: "Storm", conflict: "Conflict", iceberg: "Iceberg",
};
const SEV_BG = { 5: "#E24B4A22", 4: "#D85A3022", 3: "#EF9F2722", 2: "#1D9E7522", 1: "#37ADD422" };

function DistributionFeed({ distResult, visible, onToggle }) {
  const feed = distResult?.feed || [];
  const served = distResult?.events_served ?? 0;
  const unmet  = distResult?.events_unmet  ?? 0;

  return (
    <div style={{
      borderTop: "0.5px solid var(--color-border-tertiary)",
      background: "var(--color-background-primary)",
      flexShrink: 0,
    }}>
      {/* Toggle bar */}
      <button
        onClick={onToggle}
        style={{
          width: "100%", display: "flex", alignItems: "center", gap: 10,
          padding: "8px 18px", background: "none", border: "none",
          cursor: "pointer", borderBottom: visible ? "0.5px solid var(--color-border-tertiary)" : "none",
          fontFamily: "inherit",
        }}
      >
        <span style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase" }}>
          Distribution Feed
        </span>
        {distResult && (
          <>
            <span style={{ fontSize: 10, background: "#1D9E7520", color: "#1D9E75", borderRadius: 3, padding: "1px 6px" }}>
              {served} fully served
            </span>
            {unmet > 0 && (
              <span style={{ fontSize: 10, background: "#D85A3020", color: "#D85A30", borderRadius: 3, padding: "1px 6px" }}>
                {unmet} unmet
              </span>
            )}
            <span style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>{feed.length} allocations</span>
          </>
        )}
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: "var(--color-text-tertiary)" }}>{visible ? "▼" : "▲"}</span>
      </button>

      {visible && (
        <div style={{ maxHeight: 200, overflowY: "auto", padding: "6px 0" }}>
          {feed.length === 0 ? (
            <div style={{ padding: "16px", textAlign: "center", fontSize: 12, color: "var(--color-text-tertiary)" }}>
              {distResult ? "No allocations — all hubs empty or no eligible events." : "Move the week slider to run distribution."}
            </div>
          ) : (
            feed.map((entry, i) => (
              <div key={i} style={{
                display: "flex", alignItems: "flex-start", gap: 10,
                padding: "6px 18px",
                background: i % 2 === 0 ? "transparent" : "var(--color-background-secondary)",
                borderLeft: `3px solid ${TYPE_COLOR[entry.event_type] || "#888"}`,
              }}>
                {/* Priority rank badge */}
                <div style={{
                  minWidth: 22, height: 22, borderRadius: "50%",
                  background: SEV_BG[entry.severity] || "#88888822",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 10, fontWeight: 700,
                  color: SEV_COLOR[entry.severity] || "#888",
                  flexShrink: 0,
                }}>
                  #{entry.priority_rank}
                </div>
                {/* Hub → event */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                    <span style={{ fontSize: 11, fontWeight: 600, color: "var(--color-text-primary)", whiteSpace: "nowrap" }}>
                      {entry.hub_name}
                    </span>
                    <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>→</span>
                    <span style={{ fontSize: 11, color: "var(--color-text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 240 }}>
                      {TYPE_EMOJI[entry.event_type]} {entry.event_title}
                    </span>
                    {entry.needs_fully_met && (
                      <span style={{ fontSize: 9, background: "#1D9E7520", color: "#1D9E75", borderRadius: 3, padding: "1px 4px", fontWeight: 600, whiteSpace: "nowrap" }}>✓ FILLED</span>
                    )}
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 3, marginTop: 3 }}>
                    {Object.entries(entry.supplies).filter(([, v]) => v > 0).map(([k, v]) => (
                      <span key={k} style={{ fontSize: 9, background: "var(--color-background-secondary)", border: "0.5px solid var(--color-border-secondary)", borderRadius: 2, padding: "1px 4px", color: "var(--color-text-tertiary)" }}>
                        {AID_TYPE_LABEL[k]?.icon} {v.toLocaleString()} {k.replace(/_/g, " ")}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Week helpers ──────────────────────────────────────────────────────────
/** Returns a new Date set to Monday 00:00:00 of the week containing `date`. */
function getWeekStart(date) {
  const d = new Date(date);
  const day = d.getDay();
  const diff = day === 0 ? -6 : 1 - day; // Monday as first day of week
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + diff);
  return d;
}

function addWeeks(date, n) {
  const d = new Date(date);
  d.setDate(d.getDate() + n * 7);
  return d;
}

function weeksBetween(a, b) {
  return Math.round((b - a) / (7 * 24 * 60 * 60 * 1000));
}

function formatWeekLabel(date) {
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// ── Timeline scrubber ─────────────────────────────────────────────────────
function TimelineScrubber({ weekBounds, selectedIdx, onChange }) {
  if (!weekBounds) return null;

  const selectedStart = addWeeks(weekBounds.min, selectedIdx);
  const selectedEnd   = addWeeks(selectedStart, 1);
  const progress      = weekBounds.total > 0 ? (selectedIdx / weekBounds.total) * 100 : 100;

  return (
    <div style={{
      borderTop: "0.5px solid var(--color-border-tertiary)",
      padding: "10px 24px 12px",
      background: "var(--color-background-primary)",
      display: "flex",
      flexDirection: "column",
      gap: 6,
      flexShrink: 0,
    }}>
      {/* Labels row */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>
          {formatWeekLabel(weekBounds.min)}
        </span>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 1 }}>
          <span style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)" }}>
            {formatWeekLabel(selectedStart)} – {formatWeekLabel(selectedEnd)}
          </span>
          <span style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>
            Week {selectedIdx + 1} of {weekBounds.total + 1}
          </span>
        </div>
        <span style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>
          {formatWeekLabel(weekBounds.max)}
        </span>
      </div>

      {/* Slider */}
      <div style={{ position: "relative", height: 20, display: "flex", alignItems: "center" }}>
        {/* filled track */}
        <div style={{
          position: "absolute",
          left: 0,
          width: `${progress}%`,
          height: 3,
          background: "var(--color-text-info, #378ADD)",
          borderRadius: 2,
          pointerEvents: "none",
          zIndex: 1,
        }} />
        <input
          type="range"
          min={0}
          max={weekBounds.total}
          step={1}
          value={selectedIdx}
          onChange={e => onChange(Number(e.target.value))}
          style={{
            width: "100%",
            cursor: "pointer",
            appearance: "none",
            WebkitAppearance: "none",
            height: 3,
            background: "var(--color-border-secondary, #333)",
            borderRadius: 2,
            outline: "none",
            position: "relative",
            zIndex: 2,
          }}
        />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Main App
// ═══════════════════════════════════════════════════════════════════════════


function tryParseJson(val, fallback) {
  if (Array.isArray(val) || (val && typeof val === "object")) return val;
  if (typeof val === "string") {
    try { return JSON.parse(val); } catch { return fallback; }
  }
  return fallback;
}

function normalizeRow(row, fallbackType) {
  const lower = {};
  for (const [k, v] of Object.entries(row)) {
    lower[k.toLowerCase()] = v;
  }
  const lat = parseFloat(lower.lat) || 0;
  const lon = parseFloat(lower.lon) || 0;
  return {
    id: lower.id || "",
    source: lower.source || "",
    type: lower.type || fallbackType,
    title: lower.title || "",
    lat,
    lon,
    radius_km: parseFloat(lower.radius_km) || 0,
    location_name: lower.location_name || "",
    severity: parseInt(lower.severity) || 1,
    affected_population: parseInt(lower.affected_population) || 0,
    timestamp: lower.timestamp || "",
    status: lower.status || "active",
    action_summary: lower.action_summary || "",
    consensus_flag: lower.consensus_flag || "",
    domain_tags: tryParseJson(lower.domain_tags, []),
    allocation: tryParseJson(lower.allocation, {}),
    globe_color: lower.globe_color || "#888780",
    arc_source: tryParseJson(lower.arc_source, [0, 0]),
    arc_dest: tryParseJson(lower.arc_dest, [lat, lon]),
    arcs: tryParseJson(lower.arcs, []),
    country: lower.country || "",
    admin1: lower.admin1 || "",
    total_events: lower.total_events,
    total_fatalities: lower.total_fatalities,
  };
}

export default function App() {
  const [events, setEvents] = useState([]);
  const [selected, setSelected] = useState(null);
  const [selectedBin, setSelectedBin] = useState(null);
  const [health, setHealth] = useState(null);
  const [connected, setConnected] = useState(false);
  const [filter, setFilter] = useState("all");
  const [inventory, setInventory] = useState([]);
  const [aidEvent, setAidEvent] = useState(null);
  const [distResult, setDistResult] = useState(null);   // simulation result for current week
  const [feedOpen, setFeedOpen] = useState(true);        // distribution feed expanded?
  const [distributing, setDistributing] = useState(false);

  // Week timeline state — max week is locked to the week the site first loaded
  const loadWeekRef = useRef(getWeekStart(new Date()));
  const [weekBounds, setWeekBounds] = useState(null); // { min: Date, max: Date, total: number }
  const [selectedWeekIdx, setSelectedWeekIdx] = useState(0);

  const base = useMemo(() => apiOrigin(), []);
  const healthUrl = `${base}/health`;

  // Fetch all events once; filtering is done client-side
  useEffect(() => {
    let cancelled = false;

    fetch(`${base}/events`)
      .then((r) => r.json())
      .then((data) => {
        if (cancelled) return;
        const normalized = (data || []).map((r) => normalizeRow(r, r.type || r.TYPE || "unknown"));
        setEvents(normalized);
        setConnected(true);

        // Compute week bounds from the dataset
        const dates = normalized
          .map(ev => ev.timestamp ? new Date(ev.timestamp) : null)
          .filter(d => d && !isNaN(d));
        if (dates.length > 0) {
          const minWeek = getWeekStart(new Date(Math.min(...dates)));
          const maxWeek = loadWeekRef.current;
          const total = Math.max(weeksBetween(minWeek, maxWeek), 0);
          setWeekBounds({ min: minWeek, max: maxWeek, total });
          setSelectedWeekIdx(total); // default to current (latest) week
        }
      })
      .catch(() => {
        if (cancelled) return;
        setConnected(false);
      });

    return () => { cancelled = true; };
  }, [base]);

  // Filter events to the selected week
  const selectedWeekStart = useMemo(() => {
    if (!weekBounds) return null;
    return addWeeks(weekBounds.min, selectedWeekIdx);
  }, [weekBounds, selectedWeekIdx]);

  const weekFilteredEvents = useMemo(() => {
    if (!selectedWeekStart) return events;
    const end = addWeeks(selectedWeekStart, 1);
    return events.filter(ev => {
      if (!ev.timestamp) return false;
      const d = new Date(ev.timestamp);
      return d >= selectedWeekStart && d < end;
    });
  }, [events, selectedWeekStart]);

  // Then apply type filter on top of the week slice, sorted highest severity first
  const displayedEvents = useMemo(() => {
    const filtered = filter === "all" ? weekFilteredEvents : weekFilteredEvents.filter(ev => ev.type === filter);
    return [...filtered].sort((a, b) => (b.severity ?? 0) - (a.severity ?? 0));
  }, [weekFilteredEvents, filter]);

  // Re-run distribution whenever the week's events change (week slider moved)
  useEffect(() => {
    runDistribution(weekFilteredEvents);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weekFilteredEvents]);


  // Health polling
  useEffect(() => {
    const poll = () =>
      fetch(healthUrl).then((r) => r.json()).then(setHealth).catch(() => {});
    poll();
    const id = setInterval(poll, 10000);
    return () => clearInterval(id);
  }, [healthUrl]);

  // Inventory polling — every 60s
  useEffect(() => {
    const poll = () =>
      fetch(`${base}/inventory`).then(r => r.json()).then(setInventory).catch(() => {});
    poll();
    const id = setInterval(poll, 60_000);
    return () => clearInterval(id);
  }, [base]);

  // Distribution simulation — re-runs whenever the week's events change
  // weekFilteredEvents is defined below, so we use a ref-based approach:
  // we trigger on weekFilteredEvents via a separate effect after it's computed.
  const runDistribution = (eventsForWeek) => {
    if (!eventsForWeek || eventsForWeek.length === 0) {
      setDistResult(null);
      return;
    }
    setDistributing(true);
    fetch(`${base}/distribute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(eventsForWeek),
    })
      .then(r => r.json())
      .then(data => { setDistResult(data); setDistributing(false); })
      .catch(() => setDistributing(false));
  };

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
        {distributing && (
          <span style={{ fontSize: 10, color: "#EF9F27", background: "#EF9F2715", borderRadius: 4, padding: "2px 8px" }}>
            distributing…
          </span>
        )}
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

      <StatsBar events={weekFilteredEvents} filter={filter} onFilter={setFilter} />

      {/* Main layout */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        {/* Event list */}
        <div style={{
          width: 320, borderRight: "0.5px solid var(--color-border-tertiary)",
          display: "flex", flexDirection: "column", overflow: "hidden",
        }}>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {displayedEvents.length === 0 && (
              <div style={{ padding: 20, fontSize: 13, color: "var(--color-text-tertiary)", textAlign: "center" }}>
                {events.length === 0 ? "No events yet" : `No ${filter} events`}
              </div>
            )}
            {displayedEvents.map(ev => (
              <EventCard
                key={ev.id}
                ev={ev}
                selected={selected?.id === ev.id}
                onClick={() => { setSelectedBin(null); setSelected(ev); setAidEvent(null); }}
                onAidClick={ev => { setSelected(null); setSelectedBin(null); setAidEvent(ev); }}
                distResult={distResult}
              />
            ))}
          </div>
          <HealthPanel health={health} />
          <InventoryBar inventory={inventory} />
        </div>

        {/* Globe + detail */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
          <GlobePanel
            events={displayedEvents}
            onSelect={points => {
              // hex click passes an array; list click passes a single event
              if (Array.isArray(points)) {
                setSelectedBin(points.length > 1 ? points : null);
                setSelected(points[0] ?? null);
                setAidEvent(null);
              } else {
                setSelectedBin(null);
                setSelected(points);
                setAidEvent(null);
              }
            }}
          />
        </div>

        {/* Detail panel */}
        {selected && (
          <div style={{
            width: 340, borderLeft: "0.5px solid var(--color-border-tertiary)",
            overflowY: "auto", display: "flex", flexDirection: "column",
          }}>
            <DetailPanel
              ev={selected}
              binEvents={selectedBin}
              onSelectBinEvent={ev => setSelected(ev)}
              onClose={() => { setSelected(null); setSelectedBin(null); }}
            />
          </div>
        )}

        {/* Aid panel */}
        {aidEvent && (
          <AidPanel
            ev={aidEvent}
            distResult={distResult}
            onClose={() => setAidEvent(null)}
          />
        )}
      </div>

      {/* Distribution feed */}
      <DistributionFeed
        distResult={distResult}
        visible={feedOpen}
        onToggle={() => setFeedOpen(v => !v)}
      />

      {/* Timeline scrubber */}
      <TimelineScrubber
        weekBounds={weekBounds}
        selectedIdx={selectedWeekIdx}
        onChange={idx => { setSelectedWeekIdx(idx); }}
      />
    </div>
  );
}
