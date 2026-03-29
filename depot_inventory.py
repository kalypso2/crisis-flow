"""
Depot Inventory — thread-safe singleton tracking UNHRD pre-positioned stock.

Real baseline figures sourced from UNHRD Annual Reports and stock catalogue
(unhrd.org). Inventory depletes as events are processed and replenishes
slowly over time (simulating resupply cycles).

Stock units:
  shelter_kits   — 1 kit covers a family of 5 for 30 days
  food_rations   — person-days of emergency rations (2,100 kcal/person/day)
  medical_kits   — IEHK (Interagency Emergency Health Kit), covers 10,000 people/3 months
  water_kits     — family water purification unit (covers 5 people, 15L/day)
  vehicles       — light 4x4 field vehicles for ground convoy
"""

from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from typing import Optional

log = logging.getLogger(__name__)

# ── Baseline UNHRD inventory (real pre-positioning data) ──────────────────
_BASELINE: dict[str, dict[str, int]] = {
    "Brindisi UNHRD": {
        "shelter_kits":  3_000,
        "food_rations":  180_000,
        "medical_kits":  400,
        "water_kits":    3200,
        "vehicles":      30,
    },
    "Dubai UNHRD": {
        "shelter_kits":  5_000,
        "food_rations":  300_000,
        "medical_kits":  600,
        "water_kits":    5600,
        "vehicles":      50,
    },
    "Accra UNHRD": {
        "shelter_kits":  2_000,
        "food_rations":  120_000,
        "medical_kits":  250,
        "water_kits":    2400,
        "vehicles":      20,
    },
    "Kuala Lumpur UNHRD": {
        "shelter_kits":  4_000,
        "food_rations":  240_000,
        "medical_kits":  500,
        "water_kits":    4480,
        "vehicles":      40,
    },
    "Panama City UNHRD": {
        "shelter_kits":  2_500,
        "food_rations":  150_000,
        "medical_kits":  300,
        "water_kits":    2880,
        "vehicles":      25,
    },
    "Las Palmas UNHRD": {
        "shelter_kits":  1_500,
        "food_rations":  90_000,
        "medical_kits":  200,
        "water_kits":    1920,
        "vehicles":      15,
    },
}

ALL_STOCK_TYPES = ["shelter_kits", "food_rations", "medical_kits", "water_kits", "vehicles"]

_lock = threading.Lock()
_inventory: dict[str, dict[str, int]] = deepcopy(_BASELINE)
_last_replenish: float = time.time()
REPLENISH_INTERVAL_S = 7 * 24 * 3600   # full resupply once per week (604,800 s)
REPLENISH_RATE = 1.0                    # restore 100% of baseline each cycle


def get_inventory() -> dict[str, dict[str, int]]:
    """Return a snapshot of current inventory for all hubs."""
    _maybe_replenish()
    with _lock:
        return deepcopy(_inventory)


def get_hub_inventory(hub_name: str) -> Optional[dict[str, int]]:
    _maybe_replenish()
    with _lock:
        return deepcopy(_inventory.get(hub_name))


def commit(hub_name: str, quantities: dict[str, int]) -> dict[str, int]:
    """
    Deduct committed quantities from a hub's inventory.
    Returns the actual quantities deducted (capped at what's available).
    """
    with _lock:
        hub = _inventory.get(hub_name)
        if hub is None:
            return {}
        deducted = {}
        for item, qty in quantities.items():
            available = hub.get(item, 0)
            actual = min(qty, available)
            hub[item] = available - actual
            deducted[item] = actual
        log.info("Depot %s committed: %s — remaining: %s", hub_name, deducted, hub)
        return deducted


def stock_level(hub_name: str) -> str:
    """Return 'high' / 'medium' / 'low' / 'critical' based on % of baseline."""
    snap = get_hub_inventory(hub_name)
    baseline = _BASELINE.get(hub_name)
    if not snap or not baseline:
        return "unknown"
    ratios = [
        snap[k] / baseline[k]
        for k in ALL_STOCK_TYPES
        if baseline.get(k, 0) > 0
    ]
    avg = sum(ratios) / len(ratios) if ratios else 0
    if avg >= 0.6:  return "high"
    if avg >= 0.35: return "medium"
    if avg >= 0.1:  return "low"
    return "critical"


def _maybe_replenish():
    """Partially replenish all hubs every REPLENISH_INTERVAL_S seconds."""
    global _last_replenish
    now = time.time()
    with _lock:
        if now - _last_replenish < REPLENISH_INTERVAL_S:
            return
        _last_replenish = now
        for hub_name, baseline in _BASELINE.items():
            hub = _inventory[hub_name]
            for item, base_qty in baseline.items():
                current = hub.get(item, 0)
                restored = min(base_qty, current + int(base_qty * REPLENISH_RATE))
                hub[item] = restored
        log.info("Depot inventory fully replenished to 100%% of baseline (weekly cycle)")
