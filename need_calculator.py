"""
Need Calculator — determines aid quantities required for a crisis event.

Standards sourced from:
  - Sphere Handbook 2018 (spherestandards.org)
  - WFP Emergency Food Security Assessment guidelines
  - WHO/UNICEF WASH Cluster standards

All quantities are conservative estimates for the initial emergency phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Response window in days per severity level (initial emergency phase)
RESPONSE_WINDOW_DAYS: dict[int, int] = {
    1: 0,   # below threshold — no deployment
    2: 3,
    3: 7,
    4: 14,
    5: 30,
}

# Displacement rate: fraction of affected population likely displaced
DISPLACEMENT_RATE: dict[str, float] = {
    "earthquake": 0.30,
    "flood":      0.50,
    "cyclone":    0.40,
    "volcano":    0.60,
    "wildfire":   0.25,
    "drought":    0.10,   # slow onset, less acute displacement
    "storm":      0.20,
    "conflict":   0.45,
    "iceberg":    0.00,   # maritime — no shelter need
    "unknown":    0.20,
}


@dataclass
class AidNeed:
    """Quantities of each aid type required to respond to one event."""
    shelter_kits:  int = 0   # families needing shelter (1 kit = 1 family of 5)
    food_rations:  int = 0   # person-days of emergency food
    medical_kits:  int = 0   # IEHK kits (1 per 10,000 people / 3 months)
    water_kits:    int = 0   # family water units (1 per 5 people)
    vehicles:      int = 0   # field vehicles for ground access
    window_days:   int = 0   # planning horizon used in calculation
    displaced:     int = 0   # estimated displaced population
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "shelter_kits": self.shelter_kits,
            "food_rations":  self.food_rations,
            "medical_kits":  self.medical_kits,
            "water_kits":    self.water_kits,
            "vehicles":      self.vehicles,
            "window_days":   self.window_days,
            "displaced":     self.displaced,
            "notes":         self.notes,
        }


def calculate(
    event_type: str,
    severity: int,
    affected_population: int,
) -> AidNeed:
    """
    Compute Sphere-standard aid requirements for an event.

    Returns AidNeed with all quantities set to 0 if severity < 2
    or affected_population is unknown/zero (uses a conservative default).
    """
    window = RESPONSE_WINDOW_DAYS.get(severity, 0)
    if window == 0:
        return AidNeed(notes=["below deployment threshold"])

    # Default population if unknown
    pop = affected_population if affected_population > 0 else _default_pop(severity)
    notes = []
    if affected_population == 0:
        notes.append(f"population unknown — using severity-based estimate ({pop:,})")

    disp_rate = DISPLACEMENT_RATE.get(event_type, 0.20)
    displaced = int(pop * disp_rate)
    families  = max(1, displaced // 5)  # Sphere: family = 5 people

    # ── Sphere calculations ───────────────────────────────────────────────
    # Shelter: 1 kit per displaced family
    shelter_kits = families

    # Food: all displaced × window_days (2,100 kcal ≈ 1 ration/person/day)
    food_rations = displaced * window

    # Medical: 1 IEHK per 10,000 affected people (covers 3 months)
    medical_kits = max(1, pop // 10_000)

    # Water: 1 family unit per 5 displaced people (15L/person/day)
    water_kits = families

    # Vehicles: 1 per 5,000 displaced (field access / distribution)
    vehicles = max(1, displaced // 5_000)

    # Type-specific adjustments
    if event_type == "drought":
        food_rations = int(food_rations * 1.5)   # extended food need
        shelter_kits = shelter_kits // 3          # less acute shelter need
        notes.append("drought: food rations boosted 1.5×, shelter reduced")

    if event_type == "conflict":
        medical_kits = int(medical_kits * 2)      # trauma/surgical demand higher
        vehicles = int(vehicles * 1.5)            # more logistics needed
        notes.append("conflict: medical kits doubled, vehicles +50%")

    if event_type in ("flood", "cyclone"):
        water_kits = int(water_kits * 1.5)        # WASH critical
        notes.append("flood/cyclone: water kits boosted 1.5×")

    return AidNeed(
        shelter_kits=shelter_kits,
        food_rations=food_rations,
        medical_kits=medical_kits,
        water_kits=water_kits,
        vehicles=vehicles,
        window_days=window,
        displaced=displaced,
        notes=notes,
    )


def _default_pop(severity: int) -> int:
    """Conservative default population estimates when data is unavailable."""
    return {2: 1_000, 3: 10_000, 4: 50_000, 5: 200_000}.get(severity, 5_000)
