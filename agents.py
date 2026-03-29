"""
Agent pipeline — five agents that process each CrisisEvent in sequence.

Each agent receives a CrisisEvent and an AgentResult from the previous
stage, enriches / transforms the event, and returns its own AgentResult.
Agents do NOT mutate the event directly; the consensus engine applies
the final set of changes after reviewing all results.

Pipeline order:
  Detection → Classification → Severity → Allocation → Communication
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

from hub_agents import MasterAllocationAgent as _MasterAllocAgent

from schema import (
    CrisisEvent,
    AgentResult,
    DetectionResult,
    ClassificationResult,
    SeverityResult,
    AllocationResult,
    CommunicationResult,
)

log = logging.getLogger(__name__)


# Sources that are treated as authoritative — their red alerts override agents
AUTHORITATIVE_SOURCES = {"usgs", "noaa", "gdacs"}


# ═══════════════════════════════════════════════════════════════════════════
# Agent 1 — Detection
# ═══════════════════════════════════════════════════════════════════════════

class DetectionAgent:
    """
    Decides whether an event is a genuine crisis worth escalating.

    Accept criteria (ALL must pass):
      1. event.severity >= 2
      2. lat/lon are non-zero OR source is authoritative

    Emits: DetectionResult(accepted, reason)
    """

    def run(self, event: CrisisEvent, claude=None) -> DetectionResult:
        # ── Rule 1: minimum severity ──────────────────────────────────────
        if event.severity < 2:
            return DetectionResult(
                agent_name="detection",
                accepted=False,
                reason=f"severity {event.severity} below minimum threshold of 2",
            )

        # ── Rule 2: location sanity ───────────────────────────────────────
        if event.lat == 0.0 and event.lon == 0.0:
            if event.source not in AUTHORITATIVE_SOURCES:
                return DetectionResult(
                    agent_name="detection",
                    accepted=False,
                    reason="no location data and source is not authoritative",
                )

        result = DetectionResult(
            agent_name="detection",
            accepted=True,
            reason="passed all detection checks",
        )

        if claude is not None:
            result = self._ai_enrich(event, result, claude)

        return result

    def _ai_enrich(self, event: CrisisEvent, result: DetectionResult, claude) -> DetectionResult:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prompt = f"""You are a crisis actionability analyst for an emergency response system.
Today's date is {today}.

Event: {event.title}
Type: {event.type}
Source: {event.source}
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Severity: {event.severity}/5
Affected population: {event.affected_population:,}
Timestamp: {event.timestamp.isoformat()}

Should emergency resources be mobilized for this event?
Is it actionable (active, ongoing, urgent) or potentially stale, duplicate, or irrelevant?

Return ONLY valid JSON, no markdown:
{{"actionable": <bool>, "confidence": <float 0.0-1.0>, "reasoning": "<1-2 sentences>"}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
                raw = raw.rstrip("` \n")
            data = json.loads(raw)

            actionable = bool(data.get("actionable", True))
            confidence = float(data.get("confidence", 0.8))
            reasoning = str(data.get("reasoning", ""))

            result.reasoning = reasoning
            result.confidence = confidence

            if not actionable and confidence >= 0.9:
                result.accepted = False
                result.reason = f"AI flagged as not actionable (confidence={confidence:.2f}): {reasoning}"

        except Exception as exc:
            log.warning("Claude detection enrichment failed: %s", exc)

        return result


# ═══════════════════════════════════════════════════════════════════════════
# Agent 2 — Classification
# ═══════════════════════════════════════════════════════════════════════════

class ClassificationAgent:
    """
    Assigns a canonical event type and attaches domain tags.

    Type mapping per source:
      USGS   → always "earthquake"
      NOAA   → maps NWS event string to type (see NOAA_EVENT_MAP)
      GDACS  → reads eventtype field (EQ/FL/TC/VO/WF/DR)
      EONET  → reads categories[0].id
      ACLED  → maps event_type string

    Domain tags enrich the event for the allocation agent:
      earthquake  → ["seismic", "infrastructure_risk", "aftershock_risk"]
      flood       → ["water_rescue", "evacuation_needed"]
      cyclone     → ["wind_damage", "storm_surge", "evacuation_needed"]
      volcano     → ["ash_cloud", "lava_flow", "air_travel_risk"]
      wildfire    → ["air_quality", "evacuation_needed", "aerial_suppression"]
      drought     → ["food_security", "water_scarcity"]
      storm       → ["wind_damage", "infrastructure_risk"]
      conflict    → ["civilian_risk", "humanitarian_corridor"]
      iceberg     → ["maritime_hazard"]

    Emits: ClassificationResult(event_type, domain_tags, reason)
    """

    NOAA_TYPE_MAP = {
        "Tornado Warning":            ("storm",   ["wind_damage", "shelter_needed"]),
        "Tornado Watch":              ("storm",   ["wind_damage"]),
        "Hurricane Warning":          ("cyclone", ["wind_damage", "storm_surge", "evacuation_needed"]),
        "Hurricane Watch":            ("cyclone", ["wind_damage", "storm_surge"]),
        "Tropical Storm Warning":     ("cyclone", ["wind_damage", "storm_surge"]),
        "Flash Flood Warning":        ("flood",   ["water_rescue", "evacuation_needed"]),
        "Flood Warning":              ("flood",   ["water_rescue"]),
        "Severe Thunderstorm Warning":("storm",   ["wind_damage", "lightning_risk"]),
        "Winter Storm Warning":       ("storm",   ["road_closure", "hypothermia_risk"]),
        "Blizzard Warning":           ("storm",   ["road_closure", "hypothermia_risk"]),
    }

    DOMAIN_TAGS = {
        "earthquake": ["seismic", "infrastructure_risk", "aftershock_risk"],
        "flood":      ["water_rescue", "evacuation_needed"],
        "cyclone":    ["wind_damage", "storm_surge", "evacuation_needed"],
        "volcano":    ["ash_cloud", "lava_flow", "air_travel_risk"],
        "wildfire":   ["air_quality", "evacuation_needed", "aerial_suppression"],
        "drought":    ["food_security", "water_scarcity"],
        "storm":      ["wind_damage", "infrastructure_risk"],
        "conflict":   ["civilian_risk", "humanitarian_corridor"],
        "iceberg":    ["maritime_hazard"],
    }

    VALID_TYPES = frozenset({
        "earthquake", "flood", "cyclone", "volcano", "wildfire",
        "drought", "storm", "conflict", "iceberg", "unknown",
    })

    def run(self, event: CrisisEvent, claude=None) -> ClassificationResult:
        ev_type = event.type  # may already be set by adapter
        tags = self.DOMAIN_TAGS.get(ev_type, [])
        reason = f"type '{ev_type}' carried from adapter"

        # ── Refine NOAA type using the raw event string ───────────────────
        if event.source == "noaa":
            raw_event = event.raw.get("event", "")
            if raw_event in self.NOAA_TYPE_MAP:
                ev_type, tags = self.NOAA_TYPE_MAP[raw_event]
                reason = f"noaa event '{raw_event}' mapped to '{ev_type}'"

        # ── Refine ACLED using sub_event_type ─────────────────────────────
        if event.source == "acled":
            sub = event.raw.get("sub_event_type", "")
            if "drone" in sub.lower() or "air" in sub.lower():
                tags = ["civilian_risk", "humanitarian_corridor", "aerial_strike"]
            elif "explosion" in sub.lower():
                tags = ["civilian_risk", "infrastructure_risk"]
            reason = f"acled sub_event_type '{sub}' classified as conflict"

        result = ClassificationResult(
            agent_name="classification",
            event_type=ev_type,
            domain_tags=list(tags),
            reason=reason,
        )

        if claude is not None:
            result = self._ai_enrich(event, ev_type, result, claude)

        return result

    def _ai_enrich(self, event: CrisisEvent, ev_type: str, result: ClassificationResult, claude) -> ClassificationResult:
        existing_tags = ", ".join(result.domain_tags) if result.domain_tags else "none"
        prompt = f"""You are a disaster classification specialist for a global crisis response system.

Event: {event.title}
Source: {event.source}
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Severity: {event.severity}/5
Affected population: {event.affected_population:,}
Rule-based type: {ev_type}
Rule-based tags: {existing_tags}

Validate the type and suggest all applicable domain tags beyond the standard set.
Consider: location-specific factors, compound effects, affected infrastructure types,
secondary hazards, and humanitarian access constraints.

Return ONLY valid JSON, no markdown:
{{"event_type": "<type>", "domain_tags": ["<tag1>", ...], "confidence": <float 0.0-1.0>, "reasoning": "<1-2 sentences>"}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
                raw = raw.rstrip("` \n")
            data = json.loads(raw)

            ai_type = str(data.get("event_type", ev_type)).lower()
            ai_tags = [str(t) for t in data.get("domain_tags", [])]
            confidence = float(data.get("confidence", 0.8))
            reasoning = str(data.get("reasoning", ""))

            result.confidence = confidence
            result.reasoning = reasoning

            # Merge tags: rule-based first, then AI additions
            merged = list(result.domain_tags)
            for tag in ai_tags:
                if tag not in merged:
                    merged.append(tag)
            result.domain_tags = merged

            # Only update type if AI is confident and type is valid
            if confidence >= 0.8 and ai_type in self.VALID_TYPES:
                result.event_type = ai_type
                result.reason = f"{result.reason} | [AI type={ai_type} confidence={confidence:.2f}]"

        except Exception as exc:
            log.warning("Claude classification enrichment failed: %s", exc)

        return result


# ═══════════════════════════════════════════════════════════════════════════
# Agent 3 — Severity Scoring
# ═══════════════════════════════════════════════════════════════════════════

class SeverityAgent:
    """
    Produces a normalised 1–5 severity score using source-specific signals
    and population impact estimates.

    Scoring table:
      Earthquake (USGS mag):
        4.0–4.9 → 2 | 5.0–5.9 → 3 | 6.0–6.9 → 4 | 7.0+ → 5
        PAGER orange +1, red +2 (capped at 5)

      Storm / cyclone (NOAA):
        Minor→1 | Moderate→2 | Severe→3 | Extreme→4 | Immediate urgency +1

      Flood (GDACS alert level):
        green→2 | orange→3 | red→4

      Volcano / wildfire (GDACS / EONET):
        default 3; GDACS red → 4; GDACS orange → 3

      Drought (GDACS):
        always 2 (slow-onset, not acute)

      Conflict (ACLED fatalities):
        0→2 | 1–10→3 | 11–50→4 | 50+→5

      Population bonus:
        affected_population > 100,000 → +1 (capped at 5)

    Emits: SeverityResult(score, raw_score, reason)
    """

    def run(self, event: CrisisEvent, classification: ClassificationResult, claude=None, feedback_ctx: str = "") -> SeverityResult:
        ev_type = classification.event_type
        raw = event.raw
        score = event.severity  # adapter's initial estimate
        reason_parts = [f"adapter initial={score}"]

        if event.source == "usgs":
            mag = float(raw.get("mag") or 0)
            alert = (raw.get("alert") or "").lower()
            if mag >= 7.0:   score = 5
            elif mag >= 6.0: score = 4
            elif mag >= 5.0: score = 3
            else:            score = 2
            boost = {"orange": 1, "red": 2}.get(alert, 0)
            score = min(5, score + boost)
            reason_parts.append(f"usgs mag={mag} alert={alert or 'none'} score={score}")

        elif event.source == "noaa":
            sev_str = raw.get("severity", "Moderate")
            urgency = raw.get("urgency", "")
            base = {"Minor":1,"Moderate":2,"Severe":3,"Extreme":4,"Unknown":2}.get(sev_str, 2)
            score = min(5, base + (1 if urgency == "Immediate" else 0))
            reason_parts.append(f"noaa severity={sev_str} urgency={urgency} score={score}")

        elif event.source == "gdacs":
            alert = (raw.get("alertlevel") or "green").lower()
            score = {"green":2,"orange":3,"red":4}.get(alert, 2)
            if ev_type == "drought":
                score = 2  # drought is chronic, not acute
            # Override event.severity so the consensus weighted average uses correct base
            event.severity = score
            reason_parts.append(f"gdacs alertlevel={alert} type={ev_type} score={score}")

        elif event.source == "acled":
            fatalities = int(raw.get("fatalities") or 0)
            if fatalities == 0:        score = 2
            elif fatalities <= 10:     score = 3
            elif fatalities <= 50:     score = 4
            else:                      score = 5
            reason_parts.append(f"acled fatalities={fatalities} score={score}")

        # Population bonus
        pop_bonus = 0
        if event.affected_population > 100_000:
            pop_bonus = 1
            score = min(5, score + pop_bonus)
            reason_parts.append(f"pop_bonus={pop_bonus} affected={event.affected_population:,}")

        raw_score = float(score)
        result = SeverityResult(
            agent_name="severity",
            score=score,
            raw_score=raw_score,
            reason=" | ".join(reason_parts),
        )

        if claude is not None:
            result = self._ai_enrich(event, ev_type, score, result, claude, feedback_ctx)

        return result

    def _ai_enrich(self, event: CrisisEvent, ev_type: str, rule_score: int, result: SeverityResult, claude, feedback_ctx: str = "") -> SeverityResult:
        feedback_section = f"\n{feedback_ctx}\n" if feedback_ctx else ""
        prompt = f"""You are a crisis severity analyst for a global disaster response system.
A rule-based system scored this event {rule_score}/5. Validate and refine if needed.

Event: {event.title}
Type: {ev_type}
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Affected population: {event.affected_population:,}
Rule-based reasoning: {result.reason}
{feedback_section}
Consider: urban vs rural context, population density, infrastructure vulnerability, regional disaster history.

Return ONLY valid JSON, no markdown:
{{"score": <int 1-5>, "confidence": <float 0.0-1.0>, "reasoning": "<2 sentences>"}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
                raw = raw.rstrip("` \n")
            data = json.loads(raw)

            ai_score = int(data.get("score", rule_score))
            confidence = float(data.get("confidence", 0.8))
            reasoning = str(data.get("reasoning", ""))

            result.reasoning = reasoning
            result.confidence = confidence

            # Only adopt AI score if high-confidence and within 1 point of rule score
            if confidence >= 0.75 and abs(ai_score - rule_score) <= 1:
                result.score = ai_score
                result.reason = f"{result.reason} | [AI confidence={confidence:.2f}]"

        except Exception as exc:
            log.warning("Claude severity enrichment failed: %s", exc)

        return result


# ═══════════════════════════════════════════════════════════════════════════
# Agent 4 — Resource Allocation
# ═══════════════════════════════════════════════════════════════════════════

# Configurable resource depot locations
DEPOTS = [
    {"name": "Geneva Hub",      "lat": 46.20, "lon": 6.15},
    {"name": "Nairobi Hub",     "lat": -1.29, "lon": 36.82},
    {"name": "Bangkok Hub",     "lat": 13.76, "lon": 100.50},
    {"name": "Mexico City Hub", "lat": 19.43, "lon": -99.13},
    {"name": "Sydney Hub",      "lat": -33.87, "lon": 151.21},
    {"name": "Dubai Hub",       "lat": 25.20, "lon": 55.27},
]

RESOURCE_PLANS = {
    "earthquake": {
        5: ["search & rescue teams", "medical units", "satellite comms", "heavy lift helicopters"],
        4: ["search & rescue teams", "medical units", "satellite comms"],
        3: ["medical units", "emergency shelter", "water purification"],
        2: ["assessment team", "emergency shelter"],
    },
    "flood": {
        5: ["water rescue teams", "evacuation coordination", "emergency shelter", "food supplies"],
        4: ["water rescue teams", "evacuation coordination", "emergency shelter"],
        3: ["water rescue teams", "emergency shelter"],
        2: ["assessment team", "emergency shelter"],
    },
    "cyclone": {
        5: ["pre-positioned supplies", "evacuation routes", "search & rescue", "emergency shelter"],
        4: ["pre-positioned supplies", "evacuation routes", "emergency shelter"],
        3: ["pre-positioned supplies", "emergency shelter"],
        2: ["assessment team"],
    },
    "volcano": {
        5: ["evacuation coordination", "emergency shelter", "air quality monitoring", "medical units"],
        4: ["evacuation coordination", "emergency shelter", "air quality monitoring"],
        3: ["assessment team", "air quality monitoring"],
        2: ["assessment team"],
    },
    "wildfire": {
        5: ["aerial suppression", "evacuation coordination", "fire crews", "medical units"],
        4: ["aerial suppression", "evacuation coordination", "fire crews"],
        3: ["fire crews", "evacuation coordination"],
        2: ["assessment team", "fire crews"],
    },
    "conflict": {
        5: ["humanitarian corridor flag", "NGO alerts", "medical units", "refugee coordination"],
        4: ["humanitarian corridor flag", "NGO alerts", "medical units"],
        3: ["humanitarian corridor flag", "NGO alerts"],
        2: ["NGO alerts"],
    },
    "storm": {
        5: ["emergency shelter", "search & rescue", "power restoration teams"],
        4: ["emergency shelter", "search & rescue"],
        3: ["emergency shelter"],
        2: ["assessment team"],
    },
    "drought": {
        5: ["food aid", "water trucking", "agricultural assessment"],
        4: ["food aid", "water trucking"],
        3: ["water trucking"],
        2: ["assessment team"],
    },
    "iceberg": {
        3: ["maritime hazard warning", "coast guard alert"],
        2: ["maritime hazard warning"],
    },
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


class AllocationAgent:
    """
    Orchestrates multi-hub aid allocation using MasterAllocationAgent.

    Logic:
      1. Look up resource plan by (event_type, severity)
      2. MasterAllocationAgent solicits bids from all UNHRD/UN hubs
      3. Greedy selection fills need from highest-scoring hubs
      4. Commits inventory; returns full convoy manifest

    Emits: AllocationResult with primary depot, convoy list, and need data.
    """

    def __init__(self):
        self._master = _MasterAllocAgent()

    def run(self, event: CrisisEvent, severity: SeverityResult, claude=None, feedback_ctx: str = "") -> AllocationResult:
        ev_type = event.type
        score   = severity.score

        # Look up resource plan, fall back to sev-2 defaults
        plans     = RESOURCE_PLANS.get(ev_type, RESOURCE_PLANS["storm"])
        resources = plans.get(score, plans.get(2, ["assessment team"]))

        allocation = self._master.run(
            event_type=ev_type,
            severity=score,
            affected_population=event.affected_population,
            event_lat=event.lat,
            event_lon=event.lon,
            resource_list=resources,
            claude=claude,
            feedback_ctx=feedback_ctx,
        )

        return AllocationResult(
            agent_name="allocation",
            resources=resources,
            eta_minutes=allocation.primary_eta_min,
            depot_lat=allocation.primary_hub_lat,
            depot_lon=allocation.primary_hub_lon,
            depot_name=allocation.primary_hub_name,
            depot_org=allocation.primary_hub_org,
            transport_mode=allocation.transport_mode,
            convoys=allocation.to_dict()["convoys"],
            need=allocation.need.to_dict(),
            total_committed=allocation.total_committed,
            reason=(
                f"primary={allocation.primary_hub_name} "
                f"hubs={len(allocation.selected_bids)} "
                f"eta={allocation.primary_eta_min}min "
                f"transport={allocation.transport_mode}"
            ),
            reasoning=allocation.allocation_reasoning,
            confidence=0.9 if allocation.allocation_reasoning else 1.0,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Agent 5 — Communication
# ═══════════════════════════════════════════════════════════════════════════

TYPE_COLORS = {
    "earthquake": "#A0522D",
    "flood":      "#2196F3",
    "cyclone":    "#9C27B0",
    "volcano":    "#FF5722",
    "wildfire":   "#FF9800",
    "drought":    "#D4A017",
    "storm":      "#78909C",
    "conflict":   "#E24B4A",
    "iceberg":    "#00BCD4",
}
SEVERITY_LABELS = {1: "LOW", 2: "MODERATE", 3: "HIGH", 4: "CRITICAL", 5: "EXTREME"}


class CommunicationAgent:
    """
    Combines all upstream agent results into a human-readable action summary
    and globe rendering parameters.

    Output format:
      "[SEVERITY 4 EARTHQUAKE] M6.2 near Kathmandu, Nepal.
       Deploying: search & rescue teams, medical units, satellite comms.
       From Geneva Hub — ETA 4h 20m. Est. 280,000 people affected."

    Also emits globe rendering params:
      globe_color  — hex colour for the pulsing marker (type-mapped)
      arc_source   — (lat, lon) of the depot
      arc_dest     — (lat, lon) of the event

    Emits: CommunicationResult(summary, globe_color, arc_source, arc_dest, reason)
    """

    def run(
        self,
        event: CrisisEvent,
        detection: DetectionResult,
        classification: ClassificationResult,
        severity: SeverityResult,
        allocation: AllocationResult,
        consensus_flag: str,
        claude=None,
    ) -> CommunicationResult:

        sev_label = SEVERITY_LABELS.get(severity.score, "UNKNOWN")
        ev_type = classification.event_type.upper()
        resources_str = ", ".join(allocation.resources)
        hours, mins = divmod(allocation.eta_minutes, 60)
        eta_str = f"{hours}h {mins}m" if hours else f"{mins}m"
        pop_str = f"Est. {event.affected_population:,} people affected. " if event.affected_population else ""
        flag_str = f" [FLAG: {consensus_flag}]" if consensus_flag else ""

        num_hubs = len(allocation.convoys) if allocation.convoys else 1
        hub_str  = f"{num_hubs} hub{'s' if num_hubs > 1 else ''}" if num_hubs > 1 else allocation.depot_name
        transport_str = f" via {allocation.transport_mode}" if allocation.transport_mode else ""

        summary = (
            f"[SEVERITY {severity.score} — {sev_label} {ev_type}] "
            f"{event.title}. "
            f"Deploying: {resources_str}. "
            f"From {hub_str}{transport_str} — ETA {eta_str}. "
            f"{pop_str}"
            f"Source: {event.source.upper()}.{flag_str}"
        )

        # Build per-convoy arc entries for the globe
        arcs = [
            {
                "src_lat":     c["hub_lat"],
                "src_lon":     c["hub_lon"],
                "hub_name":    c["hub_name"],
                "hub_org":     c["hub_org"],
                "transport":   c["transport"],
                "eta_minutes": c["eta_minutes"],
            }
            for c in (allocation.convoys or [])
        ]
        if not arcs:
            arcs = [{
                "src_lat":     allocation.depot_lat,
                "src_lon":     allocation.depot_lon,
                "hub_name":    allocation.depot_name,
                "hub_org":     allocation.depot_org,
                "transport":   allocation.transport_mode,
                "eta_minutes": allocation.eta_minutes,
            }]

        result = CommunicationResult(
            agent_name="communication",
            summary=summary,
            globe_color=TYPE_COLORS.get(classification.event_type, "#888780"),
            arc_source=(allocation.depot_lat, allocation.depot_lon),
            arc_dest=(event.lat, event.lon),
            arcs=arcs,
            reason="communication summary generated",
        )

        if claude is not None:
            result = self._ai_enrich(event, classification, severity, allocation, result, claude)

        return result

    def _ai_enrich(
        self,
        event: CrisisEvent,
        classification: ClassificationResult,
        severity: SeverityResult,
        allocation: AllocationResult,
        result: CommunicationResult,
        claude,
    ) -> CommunicationResult:
        sev_label = SEVERITY_LABELS.get(severity.score, "UNKNOWN")
        resources_str = ", ".join(allocation.resources)
        hours, mins = divmod(allocation.eta_minutes, 60)
        eta_str = f"{hours}h {mins}m" if hours else f"{mins}m"

        prompt = f"""You are the communications officer for a global humanitarian crisis response system.

=== CRISIS EVENT ===
Title: {event.title}
Type: {classification.event_type.upper()} — Severity {severity.score}/5 ({sev_label})
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Affected population: {event.affected_population:,}
Resources deploying: {resources_str}
Primary hub: {allocation.depot_name} — ETA {eta_str}
Source: {event.source.upper()}

Generate two communications:
1. citizen_alert: A SHORT, plain-language alert for the affected public. Max 2 sentences. Direct and clear.
2. operational_summary: A detailed internal brief for response coordinators. Include event context, resources being deployed, logistics, and any key concerns.

Return ONLY valid JSON, no markdown:
{{"citizen_alert": "<2 sentences max>", "operational_summary": "<detailed brief>", "tone": "emergency" or "advisory"}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
                raw = raw.rstrip("` \n")
            data = json.loads(raw)

            result.citizen_alert = str(data.get("citizen_alert", ""))
            result.operational_summary = str(data.get("operational_summary", ""))
            result.reasoning = f"tone={data.get('tone', 'advisory')}"

        except Exception as exc:
            log.warning("Claude communication enrichment failed: %s", exc)

        return result
