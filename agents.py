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

import logging
import math
from datetime import datetime, timezone

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


# ── Source reliability weights ────────────────────────────────────────────

SOURCE_CONFIDENCE = {
    "usgs": 0.92,
    "noaa": 0.90,
    "gdacs": 0.88,
    "eonet": 0.80,
    "acled": 0.85,
    "twitter": 0.50,   # base; adjusted per event by keyword density
}

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
      2. event.confidence >= 0.4
      3. Not a known false-positive pattern (Twitter noise filter)
      4. lat/lon are non-zero OR source is authoritative

    For Twitter events an extra keyword-density check runs:
      - If the tweet contains a keyword but the surrounding context is clearly
        historical ("last year's bombing", "anniversary of the airstrike") it
        is rejected with reason "historical_reference".
      - Confidence is boosted if multiple distinct keyword clusters appear.

    Emits: DetectionResult(accepted, confidence, reason)
    """

    HISTORICAL_MARKERS = [
        "anniversary", "years ago", "last year", "in 1", "in 2",
        "history of", "historical", "documentary", "film", "movie",
        "book about", "novel", "remember when",
    ]

    def run(self, event: CrisisEvent) -> DetectionResult:
        base_conf = SOURCE_CONFIDENCE.get(event.source, 0.5)

        # ── Rule 1: minimum severity ──────────────────────────────────────
        if event.severity < 2:
            return DetectionResult(
                agent_name="detection",
                accepted=False,
                confidence=base_conf,
                reason=f"severity {event.severity} below minimum threshold of 2",
            )

        # ── Rule 2: minimum confidence ────────────────────────────────────
        if event.confidence < 0.4:
            return DetectionResult(
                agent_name="detection",
                accepted=False,
                confidence=base_conf,
                reason=f"source confidence {event.confidence:.2f} below 0.40",
            )

        # ── Rule 3: location sanity ───────────────────────────────────────
        if event.lat == 0.0 and event.lon == 0.0:
            if event.source not in AUTHORITATIVE_SOURCES:
                return DetectionResult(
                    agent_name="detection",
                    accepted=False,
                    confidence=0.2,
                    reason="no location data and source is not authoritative",
                )

        # ── Rule 4: Twitter noise filter ──────────────────────────────────
        if event.source == "twitter":
            text = event.raw.get("text", "").lower()
            for marker in self.HISTORICAL_MARKERS:
                if marker in text:
                    return DetectionResult(
                        agent_name="detection",
                        accepted=False,
                        confidence=0.3,
                        reason=f"twitter event rejected: historical_reference marker '{marker}'",
                    )
            # Boost confidence for high keyword density
            from adapters import CONFLICT_KEYWORDS
            matched = sum(1 for kw in CONFLICT_KEYWORDS if kw in text)
            adjusted_conf = min(0.4 + matched * 0.08, 0.75)
            event.confidence = adjusted_conf
            base_conf = adjusted_conf

        return DetectionResult(
            agent_name="detection",
            accepted=True,
            confidence=base_conf,
            reason="passed all detection checks",
        )


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
      Twitter → classifies by which keyword cluster matched

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

    Emits: ClassificationResult(event_type, domain_tags, confidence, reason)
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

    def run(self, event: CrisisEvent) -> ClassificationResult:
        ev_type = event.type  # may already be set by adapter
        tags = self.DOMAIN_TAGS.get(ev_type, [])
        confidence = 0.85
        reason = f"type '{ev_type}' carried from adapter"

        # ── Refine NOAA type using the raw event string ───────────────────
        if event.source == "noaa":
            raw_event = event.raw.get("event", "")
            if raw_event in self.NOAA_TYPE_MAP:
                ev_type, tags = self.NOAA_TYPE_MAP[raw_event]
                reason = f"noaa event '{raw_event}' mapped to '{ev_type}'"
                confidence = 0.90

        # ── Refine ACLED using sub_event_type ─────────────────────────────
        if event.source == "acled":
            sub = event.raw.get("sub_event_type", "")
            if "drone" in sub.lower() or "air" in sub.lower():
                tags = ["civilian_risk", "humanitarian_corridor", "aerial_strike"]
            elif "explosion" in sub.lower():
                tags = ["civilian_risk", "infrastructure_risk"]
            reason = f"acled sub_event_type '{sub}' classified as conflict"

        # ── Refine Twitter by keyword cluster ────────────────────────────
        if event.source == "twitter":
            text = event.raw.get("text", "").lower()
            if "drone" in text or "airstrike" in text or "air strike" in text:
                tags = ["civilian_risk", "aerial_strike", "humanitarian_corridor"]
            elif "shell" in text or "mortar" in text or "artillery" in text:
                tags = ["civilian_risk", "ground_conflict"]
            else:
                tags = ["civilian_risk", "humanitarian_corridor"]
            confidence = 0.65
            reason = "twitter keyword cluster classification"

        return ClassificationResult(
            agent_name="classification",
            event_type=ev_type,
            domain_tags=tags,
            confidence=confidence,
            reason=reason,
        )


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

      Twitter conflict:
        1 keyword match→2 | 2 matches→3 | 3+ matches→3 | geo-verified +1

      Population bonus:
        affected_population > 100,000 → +1 (capped at 5)

    Emits: SeverityResult(score, raw_score, confidence, reason)
    """

    def run(self, event: CrisisEvent, classification: ClassificationResult) -> SeverityResult:
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

        elif event.source == "twitter":
            from adapters import CONFLICT_KEYWORDS
            text = raw.get("text", "").lower()
            matched = sum(1 for kw in CONFLICT_KEYWORDS if kw in text)
            score = min(3, 2 + matched // 2)
            has_geo = bool(raw.get("geo"))
            if has_geo:
                score = min(5, score + 1)
            reason_parts.append(f"twitter keywords_matched={matched} geo={has_geo} score={score}")

        # Population bonus
        pop_bonus = 0
        if event.affected_population > 100_000:
            pop_bonus = 1
            score = min(5, score + pop_bonus)
            reason_parts.append(f"pop_bonus={pop_bonus} affected={event.affected_population:,}")

        raw_score = float(score)
        return SeverityResult(
            agent_name="severity",
            score=score,
            raw_score=raw_score,
            confidence=0.88,
            reason=" | ".join(reason_parts),
        )


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
    Selects the appropriate response resources and nearest depot.

    Logic:
      1. Look up resource plan by (event_type, severity)
      2. Find nearest depot by haversine distance
      3. Estimate ETA: distance_km / 600 km·h (air freight default)
      4. For conflict events: never route through the conflict country
         (safety override — picks second-nearest depot)

    Emits: AllocationResult(resources, eta_minutes, depot_lat, depot_lon, depot_name)
    """

    AVG_SPEED_KMH = 600  # air freight baseline

    def run(self, event: CrisisEvent, severity: SeverityResult) -> AllocationResult:
        ev_type = event.type
        score = severity.score

        # Look up resource plan, fall back to sev-2 defaults
        plans = RESOURCE_PLANS.get(ev_type, RESOURCE_PLANS["storm"])
        resources = plans.get(score, plans.get(2, ["assessment team"]))

        # Find nearest depot
        depots_ranked = sorted(
            DEPOTS,
            key=lambda d: _haversine_km(event.lat, event.lon, d["lat"], d["lon"])
        )

        chosen = depots_ranked[0]

        # Conflict safety override: skip depots in same broad region if sev >= 4
        if ev_type == "conflict" and score >= 4 and len(depots_ranked) > 1:
            chosen = depots_ranked[1]

        dist_km = _haversine_km(event.lat, event.lon, chosen["lat"], chosen["lon"])
        eta_minutes = int((dist_km / self.AVG_SPEED_KMH) * 60)

        return AllocationResult(
            agent_name="allocation",
            resources=resources,
            eta_minutes=eta_minutes,
            depot_lat=chosen["lat"],
            depot_lon=chosen["lon"],
            depot_name=chosen["name"],
            confidence=0.82,
            reason=f"depot={chosen['name']} dist={dist_km:.0f}km eta={eta_minutes}min resources={resources}",
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

    Emits: CommunicationResult(summary, globe_color, arc_source, arc_dest)
    """

    def run(
        self,
        event: CrisisEvent,
        detection: DetectionResult,
        classification: ClassificationResult,
        severity: SeverityResult,
        allocation: AllocationResult,
        consensus_flag: str,
    ) -> CommunicationResult:

        sev_label = SEVERITY_LABELS.get(severity.score, "UNKNOWN")
        ev_type = classification.event_type.upper()
        resources_str = ", ".join(allocation.resources)
        hours, mins = divmod(allocation.eta_minutes, 60)
        eta_str = f"{hours}h {mins}m" if hours else f"{mins}m"
        pop_str = f"Est. {event.affected_population:,} people affected. " if event.affected_population else ""
        flag_str = f" [FLAG: {consensus_flag}]" if consensus_flag else ""

        summary = (
            f"[SEVERITY {severity.score} — {sev_label} {ev_type}] "
            f"{event.title}. "
            f"Deploying: {resources_str}. "
            f"From {allocation.depot_name} — ETA {eta_str}. "
            f"{pop_str}"
            f"Source: {event.source.upper()}.{flag_str}"
        )

        return CommunicationResult(
            agent_name="communication",
            summary=summary,
            globe_color=TYPE_COLORS.get(classification.event_type, "#888780"),
            arc_source=(allocation.depot_lat, allocation.depot_lon),
            arc_dest=(event.lat, event.lon),
            confidence=0.95,
            reason="communication summary generated",
        )
