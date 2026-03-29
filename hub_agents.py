"""
Hub Agents — one HubAgent per UN depot + MasterAllocationAgent orchestrator.

Architecture:
  MasterAllocationAgent
    ├── HubAgent(Brindisi UNHRD)
    ├── HubAgent(Dubai UNHRD)
    ├── HubAgent(Accra UNHRD)
    ├── HubAgent(Kuala Lumpur UNHRD)
    ├── HubAgent(Panama City UNHRD)
    └── HubAgent(Las Palmas UNHRD)

Each HubAgent:
  1. Checks its current inventory
  2. Evaluates transport feasibility (air / sea / land)
  3. Submits a Bid describing what it can contribute and at what ETA

MasterAllocationAgent:
  1. Calls calculate_need() to determine total aid required
  2. Solicits bids from all HubAgents
  3. Greedily selects the best-scoring bids until need is fulfilled
  4. Returns a MultiHubAllocation with per-hub contributions and convoy arcs
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import depot_inventory as inv
from need_calculator import AidNeed, calculate

log = logging.getLogger(__name__)

# ── Hub metadata (coordinates + org + specialisations) ────────────────────

HUBS: list[dict] = [
    {
        "name": "Brindisi UNHRD",
        "org":  "UNHRD/WFP",
        "lat":  40.64,
        "lon":  17.94,
        "specializations": ["earthquake", "flood", "conflict"],
    },
    {
        "name": "Dubai UNHRD",
        "org":  "UNHRD/OCHA",
        "lat":  25.20,
        "lon":  55.27,
        "specializations": ["conflict", "drought", "flood"],
    },
    {
        "name": "Accra UNHRD",
        "org":  "UNHRD/WFP",
        "lat":   5.60,
        "lon":  -0.17,
        "specializations": ["conflict", "drought", "flood"],
    },
    {
        "name": "Kuala Lumpur UNHRD",
        "org":  "UNHRD/OCHA",
        "lat":   3.14,
        "lon": 101.69,
        "specializations": ["cyclone", "flood", "earthquake"],
    },
    {
        "name": "Panama City UNHRD",
        "org":  "UNHRD/WFP",
        "lat":   8.99,
        "lon": -79.52,
        "specializations": ["cyclone", "flood", "earthquake"],
    },
    {
        "name": "Las Palmas UNHRD",
        "org":  "UNHRD/WFP",
        "lat":  28.09,
        "lon": -15.41,
        "specializations": ["drought", "conflict", "storm"],
    },
    {
        "name": "Geneva ICRC",
        "org":  "ICRC",
        "lat":  46.20,
        "lon":   6.15,
        "specializations": ["conflict"],
    },
    {
        "name": "Nairobi OCHA",
        "org":  "OCHA",
        "lat":  -1.29,
        "lon":  36.82,
        "specializations": ["conflict", "drought", "flood"],
    },
    {
        "name": "Bangkok UNHCR",
        "org":  "UNHCR",
        "lat":  13.76,
        "lon": 100.50,
        "specializations": ["conflict", "flood", "cyclone"],
    },
    {
        "name": "Mexico City UN",
        "org":  "UN/WFP",
        "lat":  19.43,
        "lon": -99.13,
        "specializations": ["earthquake", "cyclone", "storm"],
    },
    {
        "name": "Islamabad WFP",
        "org":  "WFP",
        "lat":  33.72,
        "lon":  73.06,
        "specializations": ["earthquake", "flood", "conflict"],
    },
    {
        "name": "Dakar WFP",
        "org":  "WFP",
        "lat":  14.72,
        "lon": -17.47,
        "specializations": ["drought", "flood", "conflict"],
    },
    {
        "name": "Kyiv OCHA",
        "org":  "OCHA/UNICEF",
        "lat":  50.45,
        "lon":  30.52,
        "specializations": ["conflict"],
    },
    {
        "name": "Sydney DFAT",
        "org":  "Australia DFAT",
        "lat": -33.87,
        "lon": 151.21,
        "specializations": ["cyclone", "earthquake", "wildfire"],
    },
]

# Transport modes by distance
_AIR_MAX_KM    = 15_000   # air freight covers all distances
_SEA_COAST_KM  = 500      # within 500 km of coastline (simplified: lon check)
_LAND_MAX_KM   = 800      # ground convoy limited to 800 km

AIR_SPEED_KMH  = 700
SEA_SPEED_KMH  = 45
LAND_SPEED_KMH = 60


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, a)))


def _best_transport(dist_km: float) -> tuple[str, float]:
    """Return (mode_label, speed_kmh) for the fastest feasible transport."""
    if dist_km <= _LAND_MAX_KM:
        return "land", LAND_SPEED_KMH
    return "air", AIR_SPEED_KMH


# ── Bid dataclass ─────────────────────────────────────────────────────────

@dataclass
class Bid:
    hub_name:   str
    hub_org:    str
    hub_lat:    float
    hub_lon:    float
    transport:  str                    # air | sea | land
    dist_km:    float
    eta_minutes: int
    contribution: dict[str, int]       # what this hub can supply
    score:      float = 0.0            # higher = preferred


# ── HubAgent ─────────────────────────────────────────────────────────────

class HubAgent:
    """
    Represents a single humanitarian depot.
    Its sole job: assess capacity and bid on aid requests.
    """

    def __init__(self, hub_meta: dict):
        self.name:  str        = hub_meta["name"]
        self.org:   str        = hub_meta["org"]
        self.lat:   float      = hub_meta["lat"]
        self.lon:   float      = hub_meta["lon"]
        self.specs: list[str]  = hub_meta.get("specializations", [])

    def bid(
        self,
        event_lat: float,
        event_lon: float,
        event_type: str,
        need: AidNeed,
    ) -> Optional[Bid]:
        """
        Evaluate whether this hub can contribute and at what ETA.
        Returns None if inventory is empty or hub is too far without air.
        """
        stock = inv.get_hub_inventory(self.name)
        if not stock:
            # Hub not in inventory — use zero stock
            stock = {k: 0 for k in inv.ALL_STOCK_TYPES}

        dist_km = _haversine_km(self.lat, self.lon, event_lat, event_lon)
        transport, speed = _best_transport(dist_km)
        eta_minutes = int((dist_km / speed) * 60)

        # What this hub can offer (capped at need)
        contribution: dict[str, int] = {}
        for item in inv.ALL_STOCK_TYPES:
            needed  = getattr(need, item, 0)
            on_hand = stock.get(item, 0)
            contribution[item] = min(needed, on_hand)

        # Ignore bids that can't supply anything meaningful
        total_supply = sum(contribution.values())
        if total_supply == 0:
            return None

        # Score = coverage_ratio * specialization_bonus / (1 + normalised_dist)
        total_need = sum(getattr(need, k, 0) for k in inv.ALL_STOCK_TYPES)
        coverage = total_supply / max(1, total_need)
        spec_bonus = 1.4 if event_type in self.specs else 1.0
        dist_penalty = 1.0 + (dist_km / 10_000)
        score = (coverage * spec_bonus) / dist_penalty

        return Bid(
            hub_name=self.name,
            hub_org=self.org,
            hub_lat=self.lat,
            hub_lon=self.lon,
            transport=transport,
            dist_km=dist_km,
            eta_minutes=eta_minutes,
            contribution=contribution,
            score=score,
        )


# ── MultiHubAllocation result ─────────────────────────────────────────────

@dataclass
class MultiHubAllocation:
    """
    Full allocation result from MasterAllocationAgent.
    Contains per-hub bids and computed totals.
    """
    selected_bids:    list[Bid]
    need:             AidNeed
    total_committed:  dict[str, int] = field(default_factory=dict)
    primary_hub_name: str = ""
    primary_hub_org:  str = ""
    primary_hub_lat:  float = 0.0
    primary_hub_lon:  float = 0.0
    primary_eta_min:  int = 0
    transport_mode:   str = "air"
    resources:        list[str] = field(default_factory=list)
    allocation_reasoning: str = ""   # Claude's justification for hub selection
    allocation_concerns:  str = ""   # Claude's flagged concerns

    def to_dict(self) -> dict:
        return {
            "need": self.need.to_dict(),
            "total_committed": self.total_committed,
            "primary_depot_name": self.primary_hub_name,
            "primary_depot_org":  self.primary_hub_org,
            "primary_depot_lat":  self.primary_hub_lat,
            "primary_depot_lon":  self.primary_hub_lon,
            "primary_eta_minutes": self.primary_eta_min,
            "transport_mode": self.transport_mode,
            "resources": self.resources,
            "convoys": [
                {
                    "hub_name":    b.hub_name,
                    "hub_org":     b.hub_org,
                    "hub_lat":     b.hub_lat,
                    "hub_lon":     b.hub_lon,
                    "transport":   b.transport,
                    "dist_km":     round(b.dist_km, 1),
                    "eta_minutes": b.eta_minutes,
                    "supplies":    b.contribution,
                }
                for b in self.selected_bids
            ],
        }


# ── AI allocation helpers ─────────────────────────────────────────────────

def _format_hub_proposal(bid: Bid, event_type: str) -> str:
    """Format a hub bid as a natural-language proposal for Claude to evaluate."""
    supplies_str = ", ".join(
        f"{qty:,} {item.replace('_', ' ')}"
        for item, qty in bid.contribution.items()
        if qty > 0
    )
    return (
        f"{bid.hub_name} ({bid.hub_org}) proposes: {supplies_str or 'assessment support'} "
        f"via {bid.transport}. Distance: {bid.dist_km:,.0f} km, ETA: {bid.eta_minutes} min. "
        f"Score: {bid.score:.2f}."
    )


def _claude_select_allocation(
    bids: list[Bid],
    event_type: str,
    severity: int,
    need: AidNeed,
    claude,
    feedback_ctx: str = "",
) -> Optional[dict]:
    """
    Ask Claude to select the optimal hub combination from ranked proposals.
    Returns {"selected_hub_names": [...], "reasoning": "...", "concerns": "..."} or None.
    """
    proposals = "\n".join(
        f"  {i}. {_format_hub_proposal(b, event_type)}"
        for i, b in enumerate(bids, 1)
    )
    feedback_section = f"\n{feedback_ctx}\n" if feedback_ctx else ""
    prompt = f"""You are the allocation coordinator for a global crisis response system.

Event: {event_type} at severity {severity}/5
Total need: {json.dumps(need.to_dict())}
{feedback_section}
Hub proposals ({len(bids)} available):
{proposals}

Select the optimal combination of hubs that:
1. Covers the total need as fully as possible
2. Minimises overall ETA to affected population
3. Accounts for hub specialization and proximity
4. Avoids over-committing from a single hub

Return ONLY valid JSON, no markdown:
{{"selected_hub_names": ["<hub_name_1>", ...], "reasoning": "<2-3 sentences>", "concerns": "<gaps or logistics concerns, or empty string>"}}"""

    try:
        raw = claude.call(prompt, use_cache=True)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rstrip("` \n")
        return json.loads(raw)
    except Exception as exc:
        log.warning("_claude_select_allocation failed: %s", exc)
        return None


# ── MasterAllocationAgent ─────────────────────────────────────────────────

class MasterAllocationAgent:
    """
    Orchestrates all HubAgents to fulfil an aid request.

    Algorithm:
      1. Calculate total need with NeedCalculator
      2. Collect bids from all hubs in parallel (sequential for simplicity)
      3. Greedy selection: pick highest-scoring bids until need is filled
      4. Commit selected quantities from each hub's inventory
      5. Return MultiHubAllocation with convoy arcs for the globe
    """

    def __init__(self):
        self._hub_agents = [HubAgent(h) for h in HUBS]

    def run(
        self,
        event_type: str,
        severity: int,
        affected_population: int,
        event_lat: float,
        event_lon: float,
        resource_list: list[str],
        claude=None,
        feedback_ctx: str = "",
    ) -> MultiHubAllocation:
        need = calculate(event_type, severity, affected_population)

        # Collect and rank bids
        bids: list[Bid] = []
        for agent in self._hub_agents:
            bid = agent.bid(event_lat, event_lon, event_type, need)
            if bid:
                bids.append(bid)

        bids.sort(key=lambda b: b.score, reverse=True)

        # AI hub selection: reorder bids so Claude-preferred hubs come first
        ai_sel: Optional[dict] = None
        if claude is not None and bids:
            ai_sel = _claude_select_allocation(bids, event_type, severity, need, claude, feedback_ctx)
            if ai_sel and ai_sel.get("selected_hub_names"):
                preferred_set = set(ai_sel["selected_hub_names"])
                bids = sorted(
                    bids,
                    key=lambda b: (0 if b.hub_name in preferred_set else 1, -b.score),
                )

        # Greedy: select bids until we have enough supply or exhaust bids
        remaining: dict[str, int] = {
            k: getattr(need, k, 0) for k in inv.ALL_STOCK_TYPES
        }
        selected: list[Bid] = []
        total_committed: dict[str, int] = {k: 0 for k in inv.ALL_STOCK_TYPES}

        for bid in bids:
            if all(v <= 0 for v in remaining.values()):
                break
            # Trim bid to only what's still needed
            trimmed: dict[str, int] = {}
            for item in inv.ALL_STOCK_TYPES:
                trimmed[item] = min(bid.contribution.get(item, 0), remaining.get(item, 0))

            if sum(trimmed.values()) == 0:
                continue

            # Commit from inventory
            actual = inv.commit(bid.hub_name, trimmed)
            if sum(actual.values()) == 0:
                continue

            bid.contribution = actual
            selected.append(bid)

            for item, qty in actual.items():
                total_committed[item] = total_committed.get(item, 0) + qty
                remaining[item] = max(0, remaining.get(item, 0) - qty)

        # Fallback: ensure at least one bid selected even if inventory is 0
        if not selected and bids:
            selected = [bids[0]]

        primary = selected[0] if selected else _fallback_bid(event_lat, event_lon)

        log.info(
            "MasterAllocationAgent: %d hub(s) contributing — primary=%s eta=%dm",
            len(selected), primary.hub_name, primary.eta_minutes,
        )

        return MultiHubAllocation(
            selected_bids=selected,
            need=need,
            total_committed=total_committed,
            primary_hub_name=primary.hub_name,
            primary_hub_org=primary.hub_org,
            primary_hub_lat=primary.hub_lat,
            primary_hub_lon=primary.hub_lon,
            primary_eta_min=primary.eta_minutes,
            transport_mode=primary.transport,
            resources=resource_list,
            allocation_reasoning=ai_sel.get("reasoning", "") if ai_sel else "",
            allocation_concerns=ai_sel.get("concerns", "") if ai_sel else "",
        )


def _fallback_bid(event_lat: float, event_lon: float) -> Bid:
    """Returns a zero-contribution bid from Dubai as last resort."""
    h = HUBS[1]   # Dubai
    dist = _haversine_km(h["lat"], h["lon"], event_lat, event_lon)
    return Bid(
        hub_name=h["name"],
        hub_org=h["org"],
        hub_lat=h["lat"],
        hub_lon=h["lon"],
        transport="air",
        dist_km=dist,
        eta_minutes=int((dist / AIR_SPEED_KMH) * 60),
        contribution={k: 0 for k in inv.ALL_STOCK_TYPES},
        score=0.0,
    )
