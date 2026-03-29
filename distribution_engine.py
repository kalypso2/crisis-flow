"""
Distribution Engine — pure simulation of one week's aid allocation.

Algorithm:
  1. Reset all hub inventories to their full UNHRD baselines (week start).
  2. Sort events by severity descending (sev 5 first → sev 1 last).
  3. For each event:
       a. Calculate Sphere-standard need.
       b. Walk hubs in order of ascending distance to the event.
       c. Each hub contributes as much of each resource as it has available.
       d. Record every allocation in the chronological feed.
       e. Stop visiting hubs once all needs are met, or when all hubs are
          exhausted.
  4. Return committed quantities per event + the full allocation feed.

This is a pure function — it does NOT touch the live depot_inventory singleton.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from depot_inventory import _BASELINE, ALL_STOCK_TYPES
from hub_agents import HUBS
from need_calculator import calculate

# ── Types ─────────────────────────────────────────────────────────────────

@dataclass
class FeedEntry:
    sequence:      int               # unique 1..N — order of this line in the feed
    hub_name:      str
    hub_org:       str
    hub_lat:       float
    hub_lon:       float
    event_id:      str
    event_title:   str
    event_type:    str
    event_lat:     float
    event_lon:     float
    severity:      int
    priority_rank: int               # event's place in severity queue (same for all hubs serving that event)
    supplies:      dict[str, int]    # what this hub committed to this event
    needs_fully_met: bool            # True if all of the event's needs are now satisfied

    def to_dict(self) -> dict:
        return {
            "sequence":       self.sequence,
            "hub_name":       self.hub_name,
            "hub_org":        self.hub_org,
            "hub_lat":        self.hub_lat,
            "hub_lon":        self.hub_lon,
            "event_id":       self.event_id,
            "event_title":    self.event_title,
            "event_type":     self.event_type,
            "event_lat":      self.event_lat,
            "event_lon":      self.event_lon,
            "severity":       self.severity,
            "priority_rank":  self.priority_rank,
            "supplies":       self.supplies,
            "needs_fully_met": self.needs_fully_met,
        }


@dataclass
class SimulationResult:
    committed:    dict[str, dict[str, int]]   # event_id -> {resource: qty_committed}
    need:         dict[str, dict[str, int]]   # event_id -> {resource: qty_needed}
    feed:         list[dict]                  # chronological allocation log
    events_served: int
    events_unmet:  int


# ── Helpers ───────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, a)))


def _build_full_stock() -> dict[str, dict[str, int]]:
    """Return a fresh copy of all hubs at 100% baseline."""
    stock: dict[str, dict[str, int]] = {}
    for hub in HUBS:
        baseline = _BASELINE.get(hub["name"])
        if baseline:
            stock[hub["name"]] = deepcopy(baseline)
        else:
            stock[hub["name"]] = {k: 0 for k in ALL_STOCK_TYPES}
    return stock


# ── Core simulation ───────────────────────────────────────────────────────

def simulate_week(events: list[dict[str, Any]]) -> SimulationResult:
    """
    Run one week's priority-ordered aid distribution.

    Parameters
    ----------
    events : list of event dicts (as returned by /events endpoint).
             Each dict must have: id, type, severity, affected_population, lat, lon, title.

    Returns
    -------
    SimulationResult with committed amounts per event and the full feed.
    """
    # Week start — all hubs fully restocked
    stock = _build_full_stock()

    # Sort highest severity first; break ties by title for determinism
    sorted_events = sorted(
        events,
        key=lambda e: (-(int(e.get("severity") or 1)), str(e.get("title", ""))),
    )

    committed:  dict[str, dict[str, int]] = {}
    need_map:   dict[str, dict[str, int]] = {}
    feed:       list[dict] = []
    rank = 0
    feed_seq = 0  # unique index per feed row (multiple hubs → multiple rows per event)
    events_served = 0
    events_unmet  = 0

    for ev in sorted_events:
        ev_id    = str(ev.get("id") or "")
        ev_type  = str(ev.get("type") or "unknown")
        severity = int(ev.get("severity") or 1)
        pop      = int(ev.get("affected_population") or 0)
        lat      = float(ev.get("lat") or 0)
        lon      = float(ev.get("lon") or 0)
        title    = str(ev.get("title") or "Unknown event")

        need = calculate(ev_type, severity, pop)
        if need.window_days == 0:
            continue   # severity 1 — below deployment threshold

        rank += 1
        remaining: dict[str, int] = {
            "shelter_kits": need.shelter_kits,
            "food_rations": need.food_rations,
            "medical_kits": need.medical_kits,
            "water_kits":   need.water_kits,
            "vehicles":     need.vehicles,
        }
        need_map[ev_id]      = dict(remaining)
        committed[ev_id]     = {k: 0 for k in remaining}

        # Walk hubs closest-first
        hubs_by_dist = sorted(
            HUBS,
            key=lambda h: _haversine_km(lat, lon, h["lat"], h["lon"]),
        )

        for hub in hubs_by_dist:
            if all(v <= 0 for v in remaining.values()):
                break

            hub_stock  = stock.get(hub["name"], {})
            allocation: dict[str, int] = {}

            for item in ALL_STOCK_TYPES:
                needed    = remaining.get(item, 0)
                available = hub_stock.get(item, 0)
                give      = min(needed, available)
                if give > 0:
                    allocation[item]          = give
                    hub_stock[item]           = available - give
                    committed[ev_id][item]   += give
                    remaining[item]           = needed - give

            if any(v > 0 for v in allocation.values()):
                feed_seq += 1
                fully_met = all(v <= 0 for v in remaining.values())
                feed.append(FeedEntry(
                    sequence      = feed_seq,
                    hub_name      = hub["name"],
                    hub_org       = hub.get("org", ""),
                    hub_lat       = hub["lat"],
                    hub_lon       = hub["lon"],
                    event_id      = ev_id,
                    event_title   = title,
                    event_type    = ev_type,
                    event_lat     = lat,
                    event_lon     = lon,
                    severity      = severity,
                    priority_rank = rank,
                    supplies      = allocation,
                    needs_fully_met = fully_met,
                ).to_dict())

        fully_met = all(v <= 0 for v in remaining.values())
        if fully_met:
            events_served += 1
        else:
            events_unmet  += 1

    return SimulationResult(
        committed      = committed,
        need           = need_map,
        feed           = feed,
        events_served  = events_served,
        events_unmet   = events_unmet,
    )
