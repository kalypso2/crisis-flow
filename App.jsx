import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import Globe from "react-globe.gl";
import LandingPage, { hasLandingBeenDismissed } from "./LandingPage.jsx";

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

// Resource keys aligned with distribution_engine / need_calculator / depot_inventory
const AID_RESOURCE_KEYS = ["shelter_kits", "food_rations", "medical_kits", "water_kits", "vehicles"];

/**
 * Hub stock in the UI comes from GET /inventory (Flask main.py), which reads the same
 * in-memory singleton as agents: depot_inventory.py (_BASELINE + live commits).
 */
function coerceStockObject(obj) {
  const out = {};
  if (!obj || typeof obj !== "object") return out;
  for (const k of AID_RESOURCE_KEYS) {
    if (obj[k] == null) continue;
    const n = Number(obj[k]);
    if (Number.isFinite(n)) out[k] = n;
  }
  return out;
}

function normalizeHubInventoryRow(raw) {
  if (!raw || typeof raw !== "object") return null;
  const hub_name = raw.hub_name ?? raw.hubName;
  if (!hub_name) return null;
  const stock = coerceStockObject(raw.stock);
  const baseline = coerceStockObject(raw.baseline);
  const stock_level = typeof raw.stock_level === "string" ? raw.stock_level : "unknown";
  return { hub_name, stock, baseline, stock_level };
}

function normalizeInventoryPayload(data) {
  if (!Array.isArray(data)) return [];
  return data.map(normalizeHubInventoryRow).filter(Boolean);
}

function fmtStockCompact(n) {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
}

/** One-line preview for the hub strip (shelter kits + food rations). */
function hubStockSummaryLine(stock) {
  const sk = Number(stock?.shelter_kits ?? 0);
  const fr = Number(stock?.food_rations ?? 0);
  return `${fmtStockCompact(sk)} sh · ${fmtStockCompact(fr)} food`;
}

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

const TRANSPORT_ICON = { air: "✈️", land: "🚛", sea: "🚢" };

/** Sum supplies across convoy legs (enrichment pipeline commits). */
function mergeConvoySupplies(convoys) {
  const out = {};
  for (const c of convoys || []) {
    if (!c || typeof c !== "object") continue;
    for (const [k, v] of Object.entries(c.supplies || {})) {
      const n = Number(v) || 0;
      if (n > 0) out[k] = (out[k] || 0) + n;
    }
  }
  return out;
}

function AidPanel({ ev, distResult, onClose, onDetailsClick }) {
  if (!ev) return null;
  const alloc     = ev.allocation || {};
  const need      = alloc.need    || {};
  const resources = alloc.resources || [];
  const convoys   = alloc.convoys || [];
  const convoyMergedCommitted = mergeConvoySupplies(convoys);

  const simCommitted = (distResult?.committed || {})[ev.id] || {};
  const hasSimCommitted = Object.values(simCommitted).some(v => Number(v) > 0);
  // Prefer week-simulation commits; otherwise show enrichment hub deductions
  const committed  = hasSimCommitted ? simCommitted : convoyMergedCommitted;
  const needMap    = (distResult?.need       || {})[ev.id] || {};

  // Feed entries for this specific event
  const evFeed = (distResult?.feed || []).filter(f => f.event_id === ev.id);

  const hasNeedLine = Object.keys(AID_TYPE_LABEL).some(
    k => (needMap[k] ?? need[k] ?? 0) > 0,
  );
  const hasSphereData = (need.displaced > 0) || Object.keys(needMap).length > 0 || hasNeedLine;

  // Use simulation need if available (more accurate), fall back to pipeline need
  const needQty  = (key) => needMap[key]  ?? need[key]  ?? 0;
  const commQty  = (key) => committed[key] ?? 0;

  return (
    <div style={{ width: 360, borderLeft: "0.5px solid var(--color-border-tertiary)", display: "flex", flexDirection: "column", overflowY: "auto" }}>
      {/* Header */}
      <div style={{ padding: "16px 20px 12px", borderBottom: "0.5px solid var(--color-border-tertiary)", display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10, flexShrink: 0 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: "#1D9E75", marginBottom: 2 }}>Aid Requirements</div>
          <div style={{ fontSize: 11, color: "var(--color-text-tertiary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {TYPE_EMOJI[ev.type]} {ev.title}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 6, flexShrink: 0 }}>
          {onDetailsClick && (
            <button
              type="button"
              onClick={() => onDetailsClick(ev)}
              title="View full event details"
              style={{
                fontSize: 10, fontWeight: 600,
                marginTop: 2,
                padding: "4px 9px", borderRadius: 4,
                cursor: "pointer",
                whiteSpace: "nowrap",
                lineHeight: 1.4,
                border: "1px solid var(--color-border-secondary)",
                background: "var(--color-background-primary)",
                color: "var(--color-text-secondary)",
              }}
            >
              Details
            </button>
          )}
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--color-text-tertiary)", padding: 0 }}>×</button>
        </div>
      </div>

      {ev.citizen_alert && (
        <div style={{
          padding: "10px 16px",
          background: "#D85A3010",
          borderBottom: "0.5px solid var(--color-border-tertiary)",
          fontSize: 11,
          lineHeight: 1.45,
          color: "var(--color-text-secondary)",
        }}>
          <span style={{ fontWeight: 700, color: "#D85A30", marginRight: 6 }}>Advisory</span>
          {ev.citizen_alert}
        </div>
      )}

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

        {/* Enrichment: hub → event with exact supplies committed */}
        {convoys.length > 0 && (
          <div>
            <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase", marginBottom: 8 }}>
              Committed dispatch
            </div>
            {convoys.map((c, idx) => (
              <div
                key={idx}
                style={{
                  background: "linear-gradient(135deg, #1D9E750a 0%, var(--color-background-secondary) 100%)",
                  borderRadius: 8,
                  border: "1px solid #1D9E7530",
                  padding: "12px 14px",
                  marginBottom: idx < convoys.length - 1 ? 10 : 0,
                }}
              >
                <div style={{ fontSize: 12, lineHeight: 1.45, marginBottom: 8 }}>
                  <span style={{ fontWeight: 700, color: "#1D9E75" }}>From </span>
                  <span style={{ fontWeight: 600, color: "var(--color-text-primary)" }}>{c.hub_name || "Hub"}</span>
                  <span style={{ color: "var(--color-text-tertiary)", margin: "0 6px" }}>→</span>
                  <span style={{ fontWeight: 500, color: "var(--color-text-primary)" }}>
                    {ev.title || ev.location_name || "Disaster site"}
                  </span>
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", marginBottom: c.hub_org ? 6 : 8, fontSize: 11, color: "var(--color-text-tertiary)" }}>
                  <span>{TRANSPORT_ICON[c.transport] || "✈️"} {String(c.transport || "air")}</span>
                  {c.eta_minutes != null && Number(c.eta_minutes) > 0 && (
                    <span>ETA ~{Math.round(Number(c.eta_minutes))} min</span>
                  )}
                  {c.dist_km != null && Number(c.dist_km) > 0 && (
                    <span>{Math.round(Number(c.dist_km))} km</span>
                  )}
                </div>
                {c.hub_org && (
                  <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", marginBottom: 8 }}>{c.hub_org}</div>
                )}
                <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", marginBottom: 6 }}>Supplies deducted from this hub</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  {Object.entries(c.supplies || {}).filter(([, v]) => Number(v) > 0).map(([k, v]) => {
                    const meta = AID_TYPE_LABEL[k];
                    return (
                      <div
                        key={k}
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "center",
                          padding: "7px 10px",
                          background: "var(--color-background-primary)",
                          borderRadius: 4,
                          border: "0.5px solid var(--color-border-tertiary)",
                        }}
                      >
                        <span style={{ fontSize: 12, color: "var(--color-text-primary)" }}>
                          {meta?.icon} {meta?.label || k.replace(/_/g, " ")}
                        </span>
                        <span style={{ fontSize: 13, fontWeight: 600, color: "#1D9E75" }}>
                          {Number(v).toLocaleString()}
                          <span style={{ fontSize: 10, fontWeight: 400, color: "var(--color-text-tertiary)", marginLeft: 4 }}>
                            {meta?.unit || ""}
                          </span>
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}

        {convoys.length === 0 && alloc.recommended_depot && (
          <div style={{
            fontSize: 11,
            lineHeight: 1.45,
            color: "var(--color-text-secondary)",
            background: "var(--color-background-secondary)",
            borderRadius: 8,
            padding: "10px 12px",
            border: "0.5px solid var(--color-border-tertiary)",
          }}>
            <span style={{ fontWeight: 700, color: "#1D9E75" }}>Recommended lead hub: </span>
            {shortHubName(alloc.recommended_depot)}
            <span style={{ color: "var(--color-text-tertiary)", display: "block", marginTop: 6 }}>
              Sphere quantities below are the assessed need. Stock is committed when the enrichment pipeline runs and appears under “Committed dispatch” above.
            </span>
          </div>
        )}

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
              Hub Contributions
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {evFeed.map((entry, i) => (
                <div key={i} style={{ background: "var(--color-background-secondary)", borderRadius: 8, padding: "10px 12px" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                    <span style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)" }}>
                      {entry.hub_name}
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

        {/* C. AI Depot Recommendations */}
        {(alloc.nearest_depots || []).length > 0 && (
          <div>
            <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase", marginBottom: 8 }}>
              C. AI Depot Analysis
            </div>
            <div style={{ background: "var(--color-background-secondary)", borderRadius: 8, overflow: "hidden" }}>
              {alloc.nearest_depots.map((depot, i) => {
                const isRecommended = depot.name === alloc.recommended_depot;
                const stockColor = STOCK_COLOR[depot.stock_status] || STOCK_COLOR.unknown;
                return (
                  <div key={i} style={{
                    padding: "10px 14px",
                    borderBottom: i < alloc.nearest_depots.length - 1 ? "0.5px solid var(--color-border-tertiary)" : "none",
                    display: "flex", alignItems: "flex-start", gap: 8,
                  }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
                        <span style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)" }}>
                          {shortHubName(depot.name)}
                        </span>
                        {isRecommended && (
                          <span style={{ fontSize: 9, background: "#1D9E7520", color: "#1D9E75", border: "1px solid #1D9E7540", borderRadius: 3, padding: "1px 5px", fontWeight: 600 }}>
                            RECOMMENDED
                          </span>
                        )}
                      </div>
                      <div style={{ display: "flex", gap: 10, fontSize: 11, color: "var(--color-text-tertiary)" }}>
                        <span>✈️ {typeof depot.air_eta_hours === "number" ? depot.air_eta_hours.toFixed(1) : depot.air_eta_hours}h air</span>
                        <span>📍 {typeof depot.distance_km === "number" ? Math.round(depot.distance_km) : depot.distance_km} km</span>
                        <span style={{ color: stockColor, fontWeight: 500 }}>{(depot.stock_status || "unknown").toUpperCase()}</span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
            {alloc.logistics_notes && (
              <div style={{ fontSize: 11, color: "var(--color-text-tertiary)", fontStyle: "italic", marginTop: 6, lineHeight: 1.5 }}>
                {alloc.logistics_notes}
              </div>
            )}
          </div>
        )}

        {/* D. OCHA Funding Context */}
        {alloc.ocha_funding_usd > 0 && (
          <div style={{ background: "var(--color-background-secondary)", borderRadius: 8, padding: "10px 14px" }}>
            <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "var(--color-text-tertiary)", textTransform: "uppercase", marginBottom: 6 }}>
              D. OCHA Historical Funding
            </div>
            <div style={{ display: "flex", gap: 16 }}>
              <div>
                <div style={{ fontSize: 18, fontWeight: 600, color: "var(--color-text-primary)" }}>
                  ${(alloc.ocha_funding_usd / 1e6).toFixed(1)}M
                </div>
                <div style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>total flows</div>
              </div>
              {alloc.ocha_flow_count > 0 && (
                <div>
                  <div style={{ fontSize: 18, fontWeight: 600, color: "var(--color-text-primary)" }}>
                    {alloc.ocha_flow_count}
                  </div>
                  <div style={{ fontSize: 10, color: "var(--color-text-tertiary)" }}>funding flows</div>
                </div>
              )}
            </div>
          </div>
        )}

        <div style={{ fontSize: 10, color: "var(--color-text-tertiary)", borderTop: "0.5px solid var(--color-border-tertiary)", paddingTop: 10 }}>
          Based on Sphere Handbook 2018. Initial emergency phase only.
        </div>
      </div>
    </div>
  );
}

// ── Stock level colour ────────────────────────────────────────────────────
const STOCK_COLOR = { high: "#1D9E75", medium: "#EF9F27", low: "#D85A30", critical: "#E24B4A", unknown: "#888780" };

const HUB_INVENTORY_ROWS = [
  { key: "shelter_kits",  label: "Shelter kits",           unit: "kits",         icon: "🏕️" },
  { key: "food_rations",  label: "Food rations",           unit: "person-days",  icon: "🍱" },
  { key: "medical_kits",  label: "Medical kits (IEHK)",  unit: "kits",         icon: "🩺" },
  { key: "water_kits",    label: "Water kits",             unit: "units",        icon: "💧" },
  { key: "vehicles",      label: "Field vehicles",         unit: "vehicles",     icon: "🚛" },
];

function shortHubName(fullName) {
  return (fullName || "").replace(" UNHRD", "").replace(" UNHCR", "").replace(" OCHA", "").replace(" WFP", "");
}

// ── Hub detail panel (right) — opened from HUB INVENTORY clicks ──────────
function HubInventoryPanel({ hub, onClose }) {
  if (!hub) return null;
  const stock = hub.stock || {};
  const baseline = hub.baseline || {};
  const lvlColor = STOCK_COLOR[hub.stock_level] || STOCK_COLOR.unknown;
  const aggregatePct = {
    high: 80, medium: 50, low: 20, critical: 5, unknown: 0,
  }[hub.stock_level] ?? 0;

  return (
    <div style={{
      width: 300,
      borderLeft: "0.5px solid var(--color-border-tertiary)",
      display: "flex",
      flexDirection: "column",
      overflowY: "auto",
      flexShrink: 0,
    }}>
      <div style={{
        padding: "10px 12px 8px",
        borderBottom: "0.5px solid var(--color-border-tertiary)",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "flex-start",
      }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "var(--color-text-info, #378ADD)", marginBottom: 2 }}>
            Hub stock
          </div>
          <div style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)", lineHeight: 1.25 }}>
            {hub.hub_name}
          </div>
          <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 9, color: "var(--color-text-tertiary)" }}>Σ</span>
            <span style={{ fontSize: 10, fontWeight: 700, color: lvlColor, textTransform: "uppercase" }}>{hub.stock_level}</span>
          </div>
          <div style={{ height: 3, borderRadius: 2, background: "var(--color-border-secondary)", marginTop: 4, maxWidth: 200 }}>
            <div style={{ height: 3, borderRadius: 2, background: lvlColor, width: `${aggregatePct}%`, transition: "width 0.5s" }} />
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          style={{ background: "none", border: "none", cursor: "pointer", fontSize: 16, color: "var(--color-text-tertiary)", padding: 0, flexShrink: 0, lineHeight: 1 }}
        >
          ×
        </button>
      </div>

      <div style={{ padding: 10, display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ fontSize: 8, letterSpacing: "0.06em", color: "var(--color-text-tertiary)", textTransform: "uppercase" }}>
          On hand / baseline
        </div>
        {HUB_INVENTORY_ROWS.map(({ key, label, unit, icon }) => {
          const cur = Number(stock[key] ?? 0);
          const base = Number(baseline[key] ?? 0);
          const pct = base > 0 ? Math.min(100, Math.round((cur / base) * 100)) : (cur > 0 ? 100 : 0);
          const bar = pct >= 60 ? "#1D9E75" : pct >= 35 ? "#EF9F27" : pct >= 10 ? "#D85A30" : "#E24B4A";
          return (
            <div key={key} style={{ background: "var(--color-background-secondary)", borderRadius: 6, padding: "6px 8px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: base > 0 ? 4 : 0 }}>
                <span style={{ fontSize: 14, lineHeight: 1 }}>{icon}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 10, fontWeight: 500, color: "var(--color-text-primary)", lineHeight: 1.2 }}>{label}</div>
                  <div style={{ fontSize: 9, color: "var(--color-text-tertiary)", marginTop: 1 }}>
                    <span style={{ fontWeight: 600, color: "var(--color-text-primary)" }}>{cur.toLocaleString()}</span>
                    {base > 0 && (
                      <>
                        {" / "}
                        <span>{base.toLocaleString()}</span>
                        {" "}
                        <span style={{ fontSize: 8 }}>{unit}</span>
                        {" · "}
                        <span style={{ fontWeight: 600, color: bar }}>{pct}%</span>
                      </>
                    )}
                    {base <= 0 && <span style={{ fontSize: 8 }}> {unit}</span>}
                  </div>
                </div>
              </div>
              {base > 0 && (
                <div style={{ height: 2, borderRadius: 1, background: "var(--color-border-secondary)" }}>
                  <div style={{ height: 2, borderRadius: 1, background: bar, width: `${pct}%`, transition: "width 0.35s" }} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

const STOCK_LEVEL_SHORT = { high: "hi", medium: "med", low: "lo", critical: "!", unknown: "?" };

// ── Inventory bar — compact footer strip ──────────────────────────────────
function InventoryBar({ inventory, selectedHubName, onHubClick, loadError }) {
  if ((!inventory || inventory.length === 0) && loadError) {
    return (
      <div style={{ borderTop: "0.5px solid var(--color-border-tertiary)", padding: "8px 10px", flexShrink: 0, fontSize: 10, color: "#D85A30", lineHeight: 1.4 }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Hub stock unavailable</div>
        <div style={{ color: "var(--color-text-tertiary)" }}>
          No response from <code style={{ fontSize: 9 }}>GET /inventory</code>. Run the Flask API (e.g. port 8000) so the dashboard matches live <code style={{ fontSize: 9 }}>depot_inventory</code> used by agents.
        </div>
      </div>
    );
  }
  if (!inventory || inventory.length === 0) return null;
  return (
    <div style={{ borderTop: "0.5px solid var(--color-border-tertiary)", padding: "5px 8px 6px", flexShrink: 0 }}>
      <div style={{ fontSize: 8, letterSpacing: "0.06em", color: "var(--color-text-tertiary)", marginBottom: 4, textTransform: "uppercase" }}>
        Hub stock · live depots
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {inventory.map(hub => {
          const pct = {
            high: 80, medium: 50, low: 20, critical: 5, unknown: 0,
          }[hub.stock_level] ?? 0;
          const color = STOCK_COLOR[hub.stock_level];
          const active = selectedHubName && hub.hub_name === selectedHubName;
          const shortLv = STOCK_LEVEL_SHORT[hub.stock_level] ?? hub.stock_level?.slice(0, 3) ?? "?";
          return (
            <button
              key={hub.hub_name}
              type="button"
              onClick={() => onHubClick?.(hub)}
              title={`Open ${hub.hub_name} inventory`}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 1,
                width: "100%",
                padding: "2px 4px",
                margin: 0,
                border: active ? `1px solid ${color}66` : "1px solid transparent",
                borderRadius: 4,
                background: active ? `${color}10` : "transparent",
                cursor: "pointer",
                textAlign: "left",
                fontFamily: "inherit",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 4, minHeight: 14 }}>
                <span style={{ fontSize: 9, color: "var(--color-text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                  {shortHubName(hub.hub_name)}
                </span>
                <span style={{ fontSize: 8, color, fontWeight: 700, flexShrink: 0, textTransform: "uppercase" }}>
                  {shortLv}
                </span>
              </div>
              <div style={{ fontSize: 8, color: "var(--color-text-tertiary)", fontVariantNumeric: "tabular-nums", marginTop: 1 }}>
                {hubStockSummaryLine(hub.stock)}
              </div>
              <div style={{ height: 2, borderRadius: 1, background: "var(--color-border-secondary)" }}>
                <div style={{ height: 2, borderRadius: 1, background: color, width: `${pct}%`, transition: "width 0.5s" }} />
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Detail panel: omit radius for feeds where it is a fixed placeholder, not a derived impact zone.
const DETAIL_HIDE_RADIUS_SOURCES = new Set(["eonet", "gdacs", "noaa"]);

function CitizenAlertBanner({ text }) {
  if (!text) return null;
  return (
    <div style={{
      marginBottom: 16,
      padding: "12px 14px",
      borderRadius: 8,
      border: "1px solid #D85A3044",
      background: "#D85A3012",
      color: "var(--color-text-primary)",
      fontSize: 13,
      lineHeight: 1.55,
    }}>
      <div style={{ fontSize: 10, letterSpacing: "0.08em", color: "#D85A30", textTransform: "uppercase", marginBottom: 6, fontWeight: 600 }}>
        Public advisory
      </div>
      {text}
    </div>
  );
}

function CollapsiblePanel({ title, children }) {
  return (
    <details style={{
      marginBottom: 12,
      borderRadius: 8,
      background: "var(--color-background-secondary)",
      border: "0.5px solid var(--color-border-tertiary)",
    }}>
      <summary style={{
        cursor: "pointer",
        padding: "10px 12px",
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: "var(--color-text-tertiary)",
        userSelect: "none",
      }}>
        {title}
      </summary>
      <div style={{
        padding: "10px 12px 12px",
        borderTop: "0.5px solid var(--color-border-tertiary)",
      }}>
        {children}
      </div>
    </details>
  );
}

function AgentReasoningPanel({ ar }) {
  if (!ar || typeof ar !== "object") return null;
  const wc = ar.weather_context;
  const imp = ar.impact;
  const cr = Array.isArray(ar.compound_risks) ? ar.compound_risks : [];
  const aid = ar.aid_context;
  const hasAny = (wc && typeof wc === "object")
    || (imp && typeof imp === "object")
    || cr.length > 0
    || (aid && typeof aid === "object" && Object.keys(aid).length > 0);
  if (!hasAny) return null;

  return (
    <div style={{ fontSize: 12, lineHeight: 1.55, color: "var(--color-text-secondary)" }}>
      {wc && typeof wc === "object" && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, color: "var(--color-text-primary)", marginBottom: 6 }}>Weather context</div>
          {wc.forecast_risk && (
            <div style={{ marginBottom: 4 }}>Forecast risk: <strong>{wc.forecast_risk}</strong></div>
          )}
          {wc.forecast_summary && <div style={{ marginBottom: 4 }}>{wc.forecast_summary}</div>}
          {wc.historical_context && (
            <div style={{ fontSize: 11, color: "var(--color-text-tertiary)", marginBottom: 4 }}>{wc.historical_context}</div>
          )}
          {wc.current && typeof wc.current === "object" && (
            <div style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>
              Current: {wc.current.description ?? "—"}
              {wc.current.temp_c != null && ` · ${wc.current.temp_c}°C`}
              {wc.current.wind_kmh != null && ` · wind ${wc.current.wind_kmh} km/h`}
              {wc.current.precipitation_mm != null && ` · ${wc.current.precipitation_mm} mm precip`}
            </div>
          )}
        </div>
      )}
      {imp && typeof imp === "object" && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, color: "var(--color-text-primary)", marginBottom: 6 }}>Impact assessment</div>
          {imp.impact_severity && <div style={{ marginBottom: 4 }}>Severity: {imp.impact_severity}</div>}
          {imp.affected_population != null && imp.affected_population !== "" && (
            <div style={{ marginBottom: 4 }}>Affected population (estimate): {Number(imp.affected_population).toLocaleString()}</div>
          )}
          {Array.isArray(imp.infrastructure_at_risk) && imp.infrastructure_at_risk.length > 0 && (
            <div style={{ marginBottom: 4 }}>
              Infrastructure at risk: {imp.infrastructure_at_risk.join(", ")}
            </div>
          )}
          {imp.historical_precedent && (
            <div style={{ fontSize: 11, color: "var(--color-text-tertiary)", marginBottom: 4 }}>{imp.historical_precedent}</div>
          )}
          {(imp.news_headline || imp.news_url) && (
            <div style={{ fontSize: 11 }}>
              {imp.news_headline && <span>{imp.news_headline}</span>}
              {imp.news_url && (
                <span>
                  {" "}
                  <a href={imp.news_url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--color-text-info)" }}>Link</a>
                </span>
              )}
            </div>
          )}
        </div>
      )}
      {cr.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, color: "var(--color-text-primary)", marginBottom: 6 }}>Compound risks</div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {cr.map((x, i) => (
              <li key={i} style={{ marginBottom: 4 }}>
                {typeof x === "object" && x !== null
                  ? (x.description || x.type || JSON.stringify(x))
                  : String(x)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {aid && typeof aid === "object" && Object.keys(aid).length > 0 && (
        <div>
          <div style={{ fontWeight: 600, color: "var(--color-text-primary)", marginBottom: 6 }}>Aid context (agent)</div>
          <pre style={{
            margin: 0,
            fontSize: 10,
            fontFamily: "ui-monospace, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            color: "var(--color-text-tertiary)",
          }}>
            {JSON.stringify(aid, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

// ── Detail panel ──────────────────────────────────────────────────────────
function DetailPanel({ ev, binEvents, onSelectBinEvent, onClose, distResult, onAidClick }) {
  if (!ev) return (
    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--color-text-tertiary)", fontSize: 13 }}>
      Select an event to see details
    </div>
  );

  const alloc = ev.allocation || {};
  const { style: detailAidBtnStyle, avgPct: detailAidAvgPct } = getAidButtonStyle(ev, distResult);
  const detailAidTitle = detailAidAvgPct == null
    ? "View aid requirements (run week simulation for fill status)"
    : `View aid requirements — average supply fill: ${detailAidAvgPct}%`;

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
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <SevBadge sev={ev.severity} />
            {onAidClick && (
              <button
                type="button"
                onClick={() => onAidClick(ev)}
                title={detailAidTitle}
                style={{
                  fontSize: 10, fontWeight: 600,
                  padding: "2px 7px", borderRadius: 4,
                  cursor: "pointer",
                  whiteSpace: "nowrap",
                  lineHeight: 1.4,
                  ...detailAidBtnStyle,
                }}
              >
                Aid
              </button>
            )}
            <span style={{ fontSize: 11, color: "var(--color-text-tertiary)", textTransform: "uppercase" }}>{ev.type}</span>
            <span style={{ fontSize: 11, color: "var(--color-text-tertiary)" }}>via {ev.source?.toUpperCase()}</span>
            <FlagBadge flag={ev.consensus_flag} />
            {ev.enrichment_status && (
              <span style={{
                fontSize: 9,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                padding: "2px 6px",
                borderRadius: 4,
                background: ev.enrichment_status === "complete" ? "#1D9E7520" : "var(--color-background-secondary)",
                color: ev.enrichment_status === "complete" ? "#1D9E75" : "var(--color-text-tertiary)",
                border: `1px solid ${ev.enrichment_status === "complete" ? "#1D9E7544" : "var(--color-border-secondary)"}`,
              }}>
                {ev.enrichment_status.replace(/_/g, " ")}
              </span>
            )}
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--color-text-tertiary)", padding: 0 }}>×</button>
      </div>

      <CitizenAlertBanner text={ev.citizen_alert} />

      <Section title="Action summary">
        <p style={{ fontSize: 13, lineHeight: 1.6, color: "var(--color-text-secondary)", margin: 0 }}>
          {ev.action_summary || "—"}
        </p>
      </Section>

      {ev.operational_summary?.trim() && (
        <Section title="Operational summary">
          <p style={{ fontSize: 13, lineHeight: 1.6, color: "var(--color-text-secondary)", margin: 0 }}>
            {ev.operational_summary}
          </p>
        </Section>
      )}

      {ev.enrichment_summary?.trim() && (
        <Section title="Enrichment summary">
          <p style={{ fontSize: 13, lineHeight: 1.6, color: "var(--color-text-secondary)", margin: 0 }}>
            {ev.enrichment_summary}
          </p>
        </Section>
      )}

      {ev.agent_reasoning && (
        <CollapsiblePanel title="AI reasoning & transparency">
          <AgentReasoningPanel ar={ev.agent_reasoning} />
        </CollapsiblePanel>
      )}

      {ev.weather_context_record && (
        <CollapsiblePanel title="Weather context (ingest column)">
          <pre style={{
            margin: 0,
            fontSize: 11,
            fontFamily: "ui-monospace, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            color: "var(--color-text-secondary)",
          }}>
            {JSON.stringify(ev.weather_context_record, null, 2)}
          </pre>
        </CollapsiblePanel>
      )}


      <Section title="Location">
        <Row label="Lat / Lon">{ev.lat?.toFixed(4)}, {ev.lon?.toFixed(4)}</Row>
        {!DETAIL_HIDE_RADIUS_SOURCES.has(String(ev.source || "").toLowerCase()) && (
          <Row label="Radius">{ev.radius_km} km</Row>
        )}
        {ev.location_name && <Row label="Area">{ev.location_name}</Row>}
      </Section>

      {alloc.depot_name && (
        <Section title="Aid deployment">
          <Row label="Lead hub">{alloc.depot_name}</Row>
          {alloc.depot_org && <Row label="Organisation">{alloc.depot_org}</Row>}
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

      {(alloc.convoys || []).length > 0 && (
        <Section title={alloc.convoys.length > 1 ? `Convoys (${alloc.convoys.length} hubs)` : "Committed dispatch"}>
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
        <Row label="Timestamp">{ev.timestamp ? new Date(ev.timestamp).toLocaleString() : "—"}</Row>
        <Row label="Status">{ev.status}</Row>
        {ev.ingested_at && (
          <Row label="Ingested">{String(ev.ingested_at)}</Row>
        )}
        {ev.enriched_at && (
          <Row label="Enriched at">{String(ev.enriched_at)}</Row>
        )}
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

// ── 3D globe ──────────────────────────────────────────────────────────────
function dominantType(hexBin) {
  const counts = {};
  hexBin.points.forEach(p => {
    counts[p.type] = (counts[p.type] || 0) + 1;
  });
  const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  return sorted.length > 0 ? sorted[0][0] : "unknown";
}

/** One great-circle arc per distribution feed row; staggered reveal in GlobePanel. */
const SIM_ARC_STAGGER_MS = 420;

function feedRowToSimulationArc(row) {
  const supplies = row?.supplies || {};
  const hasGive = Object.values(supplies).some(v => Number(v) > 0);
  if (!hasGive) return null;
  const hLat = row.hub_lat;
  const hLon = row.hub_lon;
  const eLat = row.event_lat;
  const eLon = row.event_lon;
  if (![hLat, hLon, eLat, eLon].every(n => Number.isFinite(n))) return null;
  return {
    startLat: hLat,
    startLng: hLon,
    endLat: eLat,
    endLng: eLon,
    color: TYPE_COLOR[row.event_type] || "#888888",
    sequence: row.sequence,
  };
}

function GlobePanel({ events, distResult, eventTypeFilter = "all", onSelect }) {
  const globeRef = useRef(null);

  const validEvents = useMemo(() =>
    events.filter(ev => Number.isFinite(ev.lat) && Number.isFinite(ev.lon)),
  [events]);

  const allSimulationArcs = useMemo(() => {
    const feed = distResult?.feed;
    if (!Array.isArray(feed) || feed.length === 0) return [];
    let rows = [...feed].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
    if (eventTypeFilter !== "all") {
      rows = rows.filter(r => r.event_type === eventTypeFilter);
    }
    const out = [];
    for (const row of rows) {
      const arc = feedRowToSimulationArc(row);
      if (arc) out.push(arc);
    }
    return out;
  }, [distResult, eventTypeFilter]);

  const [visibleArcCount, setVisibleArcCount] = useState(0);

  useEffect(() => {
    if (allSimulationArcs.length === 0) {
      setVisibleArcCount(0);
      return undefined;
    }
    setVisibleArcCount(1);
    const max = allSimulationArcs.length;
    if (max <= 1) return undefined;
    let n = 1;
    const id = setInterval(() => {
      n += 1;
      setVisibleArcCount(n);
      if (n >= max) clearInterval(id);
    }, SIM_ARC_STAGGER_MS);
    return () => clearInterval(id);
  }, [allSimulationArcs]);

  const arcsData = useMemo(
    () => allSimulationArcs.slice(0, visibleArcCount),
    [allSimulationArcs, visibleArcCount],
  );

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
          width={window.innerWidth - 760}
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
        Events: {validEvents.length}
        {allSimulationArcs.length > 0
          ? ` · Simulation arcs: ${arcsData.length}/${allSimulationArcs.length}`
          : " · Run distribution for arcs"}
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

function DistributionFeed({ distResult, visible, onToggle, events, onFeedRowClick }) {
  const feed = distResult?.feed || [];
  const served = distResult?.events_served ?? 0;
  const unmet  = distResult?.events_unmet  ?? 0;
  const rowClickable = typeof onFeedRowClick === "function" && Array.isArray(events);

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
            feed.map((entry, i) => {
              const seq = entry.sequence ?? i + 1;
              const openAid = () => {
                if (!rowClickable) return;
                const id = entry.event_id;
                const ev = events.find(e => e.id === id || String(e.id) === String(id));
                if (ev) onFeedRowClick(ev);
              };
              return (
              <div
                key={seq}
                role={rowClickable ? "button" : undefined}
                tabIndex={rowClickable ? 0 : undefined}
                onClick={rowClickable ? openAid : undefined}
                onKeyDown={rowClickable ? (e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    openAid();
                  }
                } : undefined}
                title={rowClickable ? "Open aid requirements for this event" : undefined}
                style={{
                display: "flex", alignItems: "flex-start", gap: 10,
                padding: "6px 18px",
                background: i % 2 === 0 ? "transparent" : "var(--color-background-secondary)",
                borderLeft: `3px solid ${TYPE_COLOR[entry.event_type] || "#888"}`,
                cursor: rowClickable ? "pointer" : undefined,
                }}
                onMouseDown={rowClickable ? (e) => { if (e.detail > 1) e.preventDefault(); } : undefined}
              >
                {/* Unique allocation order (#1, #2, …); color = event severity */}
                <div style={{
                  minWidth: 22, height: 22, borderRadius: "50%",
                  background: SEV_BG[entry.severity] || "#88888822",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 10, fontWeight: 700,
                  color: SEV_COLOR[entry.severity] || "#888",
                  flexShrink: 0,
                }}>
                  #{seq}
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
                  <div style={{ fontSize: 8, color: "var(--color-text-tertiary)", marginTop: 2 }}>
                    Severity queue #{entry.priority_rank}
                  </div>
                </div>
              </div>
              );
            })
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

/** Strip legacy need.notes lines about imputed population (stale API / DB). */
function scrubAllocationNeedNotes(allocation) {
  if (!allocation || typeof allocation !== "object") return allocation;
  const need = allocation.need;
  if (!need || typeof need !== "object" || !Array.isArray(need.notes)) return allocation;
  const filtered = need.notes.filter(n => {
    if (typeof n !== "string") return true;
    const low = n.toLowerCase();
    return !(low.includes("population unknown") && low.includes("severity-based estimate"));
  });
  if (filtered.length === need.notes.length) return allocation;
  return { ...allocation, need: { ...need, notes: filtered } };
}

/** Parse JSON object from API (string or object); return null if missing or invalid. */
function parseJsonObject(val) {
  if (!val) return null;
  if (typeof val === "object" && !Array.isArray(val)) return val;
  if (typeof val === "string") {
    try {
      const o = JSON.parse(val);
      return o && typeof o === "object" && !Array.isArray(o) ? o : null;
    } catch {
      return null;
    }
  }
  return null;
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
    allocation: scrubAllocationNeedNotes(tryParseJson(lower.allocation, {})),
    globe_color: lower.globe_color || "#888780",
    arc_source: tryParseJson(lower.arc_source, [0, 0]),
    arc_dest: tryParseJson(lower.arc_dest, [lat, lon]),
    arcs: tryParseJson(lower.arcs, []),
    country: lower.country || "",
    admin1: lower.admin1 || "",
    total_events: lower.total_events,
    total_fatalities: lower.total_fatalities,
    agent_reasoning: parseJsonObject(lower.agent_reasoning),
    operational_summary: lower.operational_summary || "",
    citizen_alert: String(lower.citizen_alert || "").trim(),
    enrichment_status: lower.enrichment_status || "",
    enrichment_summary: lower.enrichment_summary || "",
    enriched_at: lower.enriched_at || "",
    ingested_at: lower.ingested_at || "",
    weather_context_record: parseJsonObject(lower.weather_context),
  };
}

export default function App() {
  const [landingDismissed, setLandingDismissed] = useState(() => hasLandingBeenDismissed());

  const [events, setEvents] = useState([]);
  const [selected, setSelected] = useState(null);
  const [selectedBin, setSelectedBin] = useState(null);
  const [connected, setConnected] = useState(false);
  const [filter, setFilter] = useState("all");
  const [inventory, setInventory] = useState([]);
  const [inventoryLoadError, setInventoryLoadError] = useState(false);
  const [aidEvent, setAidEvent] = useState(null);
  const [hubPanelHub, setHubPanelHub] = useState(null); // hub detail panel from HUB INVENTORY
  const [distResult, setDistResult] = useState(null);   // simulation result for current week
  const [feedOpen, setFeedOpen] = useState(true);        // distribution feed expanded?
  const [distributing, setDistributing] = useState(false);
  const [pipelineBusy, setPipelineBusy] = useState(false);
  const [pipeStatus, setPipeStatus] = useState(null);

  // Week timeline state — max week is locked to the week the site first loaded
  const loadWeekRef = useRef(getWeekStart(new Date()));
  const [weekBounds, setWeekBounds] = useState(null); // { min: Date, max: Date, total: number }
  const [selectedWeekIdx, setSelectedWeekIdx] = useState(0);

  const base = useMemo(() => apiOrigin(), []);

  const refetchEvents = useCallback(() => {
    fetch(`${base}/events`)
      .then((r) => r.json())
      .then((data) => {
        const normalized = (data || []).map((r) => normalizeRow(r, r.type || r.TYPE || "unknown"));
        setEvents(normalized);
        setConnected(true);
        const dates = normalized
          .map(ev => ev.timestamp ? new Date(ev.timestamp) : null)
          .filter(d => d && !isNaN(d));
        if (dates.length > 0) {
          const minWeek = getWeekStart(new Date(Math.min(...dates)));
          const maxWeek = loadWeekRef.current;
          const total = Math.max(weeksBetween(minWeek, maxWeek), 0);
          const latestWeek = getWeekStart(new Date(Math.max(...dates)));
          let idx = weeksBetween(minWeek, latestWeek);
          if (idx < 0) idx = 0;
          if (idx > total) idx = total;
          setWeekBounds({ min: minWeek, max: maxWeek, total });
          setSelectedWeekIdx(idx);
        }
      })
      .catch(() => setConnected(false));
  }, [base]);

  const refetchPipelineStatus = useCallback(() => {
    fetch(`${base}/pipeline/status`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d) setPipeStatus(d); })
      .catch(() => setPipeStatus(null));
  }, [base]);

  const refetchInventoryNow = useCallback(() => {
    fetch(`${base}/inventory`)
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then((data) => {
        setInventory(normalizeInventoryPayload(data));
        setInventoryLoadError(false);
      })
      .catch(() => {
        setInventory([]);
        setInventoryLoadError(true);
      });
  }, [base]);

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
          const latestWeek = getWeekStart(new Date(Math.max(...dates)));
          let idx = weeksBetween(minWeek, latestWeek);
          if (idx < 0) idx = 0;
          if (idx > total) idx = total;
          setWeekBounds({ min: minWeek, max: maxWeek, total });
          setSelectedWeekIdx(idx);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setConnected(false);
      });

    return () => { cancelled = true; };
  }, [base]);

  useEffect(() => {
    refetchPipelineStatus();
  }, [base, refetchPipelineStatus, connected]);

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


  // Inventory polling — every 60s (GET /inventory → depot_inventory singleton)
  useEffect(() => {
    const poll = () =>
      fetch(`${base}/inventory`)
        .then((r) => {
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        })
        .then((data) => {
          setInventory(normalizeInventoryPayload(data));
          setInventoryLoadError(false);
        })
        .catch(() => {
          setInventory([]);
          setInventoryLoadError(true);
        });
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

  if (!landingDismissed) {
    return <LandingPage onEnter={() => setLandingDismissed(true)} />;
  }

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
        {pipeStatus && !pipeStatus.auto_pipeline && (
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            <span style={{ fontSize: 10, color: "var(--color-text-tertiary)" }} title="Queue depth for agent pipeline">
              queue {pipeStatus.queue_depth ?? "—"}
            </span>
            <button
              type="button"
              disabled={pipelineBusy || !connected}
              onClick={() => {
                setPipelineBusy(true);
                fetch(`${base}/pipeline/poll-sources`, { method: "POST" })
                  .then((r) => r.json())
                  .then(() => {
                    refetchPipelineStatus();
                    refetchEvents();
                  })
                  .finally(() => setPipelineBusy(false));
              }}
              style={{
                fontSize: 10,
                padding: "4px 10px",
                borderRadius: 4,
                border: "1px solid var(--color-border-secondary)",
                background: "var(--color-background-secondary)",
                color: "var(--color-text-secondary)",
                cursor: pipelineBusy || !connected ? "not-allowed" : "pointer",
                fontFamily: "inherit",
              }}
            >
              Poll sources
            </button>
            <button
              type="button"
              disabled={pipelineBusy || !connected}
              onClick={() => {
                setPipelineBusy(true);
                fetch(`${base}/pipeline/process-next`, { method: "POST" })
                  .then((r) => r.json())
                  .then(() => {
                    refetchPipelineStatus();
                    refetchEvents();
                    refetchInventoryNow();
                  })
                  .finally(() => setPipelineBusy(false));
              }}
              style={{
                fontSize: 10,
                padding: "4px 10px",
                borderRadius: 4,
                border: "1px solid #378ADD44",
                background: "var(--color-background-info)",
                color: "var(--color-text-info, #378ADD)",
                cursor: pipelineBusy || !connected ? "not-allowed" : "pointer",
                fontFamily: "inherit",
                fontWeight: 600,
              }}
            >
              Process next event
            </button>
          </div>
        )}
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
          width: 400, borderRight: "0.5px solid var(--color-border-tertiary)",
          display: "flex", flexDirection: "column", overflow: "hidden",
          minWidth: 0,
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
                onClick={() => { setSelectedBin(null); setSelected(ev); setAidEvent(null); setHubPanelHub(null); }}
                onAidClick={ev => { setSelected(null); setSelectedBin(null); setAidEvent(ev); setHubPanelHub(null); }}
                distResult={distResult}
              />
            ))}
          </div>
          <InventoryBar
            inventory={inventory}
            loadError={inventoryLoadError}
            selectedHubName={hubPanelHub?.hub_name}
            onHubClick={h => {
              setSelected(null);
              setSelectedBin(null);
              setAidEvent(null);
              setHubPanelHub(h);
            }}
          />
        </div>

        {/* Globe + detail */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
          <GlobePanel
            events={displayedEvents}
            distResult={distResult}
            eventTypeFilter={filter}
            onSelect={points => {
              // hex click passes an array; list click passes a single event
              if (Array.isArray(points)) {
                setSelectedBin(points.length > 1 ? points : null);
                setSelected(points[0] ?? null);
                setAidEvent(null);
                setHubPanelHub(null);
              } else {
                setSelectedBin(null);
                setSelected(points);
                setAidEvent(null);
                setHubPanelHub(null);
              }
            }}
          />
        </div>

        {/* Exactly one right panel: hub inventory, detail, or aid */}
        {hubPanelHub ? (
          <HubInventoryPanel
            hub={hubPanelHub}
            onClose={() => setHubPanelHub(null)}
          />
        ) : selected ? (
          <div style={{
            width: 340, borderLeft: "0.5px solid var(--color-border-tertiary)",
            overflowY: "auto", display: "flex", flexDirection: "column",
          }}>
            <DetailPanel
              ev={selected}
              binEvents={selectedBin}
              onSelectBinEvent={ev => { setSelected(ev); setHubPanelHub(null); }}
              onClose={() => { setSelected(null); setSelectedBin(null); }}
              distResult={distResult}
              onAidClick={ev => {
                setSelected(null);
                setSelectedBin(null);
                setHubPanelHub(null);
                setAidEvent(ev);
              }}
            />
          </div>
        ) : aidEvent ? (
          <AidPanel
            ev={aidEvent}
            distResult={distResult}
            onClose={() => setAidEvent(null)}
            onDetailsClick={ev => {
              setAidEvent(null);
              setHubPanelHub(null);
              setSelectedBin(null);
              setSelected(ev);
            }}
          />
        ) : null}
      </div>

      {/* Distribution feed */}
      <DistributionFeed
        distResult={distResult}
        visible={feedOpen}
        onToggle={() => setFeedOpen(v => !v)}
        events={events}
        onFeedRowClick={ev => {
          setSelected(null);
          setSelectedBin(null);
          setHubPanelHub(null);
          setAidEvent(ev);
        }}
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
