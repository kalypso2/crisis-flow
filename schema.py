"""
CrisisEvent — the shared data contract for the entire pipeline.

Every adapter normalises its source into this schema before anything
enters the event queue. Every agent reads from and writes to this object.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
import uuid


@dataclass
class CrisisEvent:
    # ── Identity ──────────────────────────────────────────────────────────
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str = ""          # usgs | noaa | gdacs | eonet | acled | twitter

    # ── Classification ────────────────────────────────────────────────────
    type: str = "unknown"     # earthquake | flood | cyclone | volcano |
                              # wildfire | drought | storm | conflict | iceberg
    domain_tags: list[str] = field(default_factory=list)

    # ── Location ──────────────────────────────────────────────────────────
    lat: float = 0.0
    lon: float = 0.0
    radius_km: float = 10.0
    location_name: str = ""

    # ── Severity ──────────────────────────────────────────────────────────
    severity: int = 1         # 1 (low) – 5 (critical)
    affected_population: int = 0

    # ── Timing ────────────────────────────────────────────────────────────
    timestamp: datetime = field(default_factory=datetime.utcnow)
    status: str = "active"    # active | resolved

    # ── Agent pipeline outputs ────────────────────────────────────────────
    title: str = ""
    action_summary: str = ""
    consensus_flag: str = ""  # "" | LOW_CONFIDENCE | FALLBACK_USED
    allocation: dict[str, Any] = field(default_factory=dict)

    # ── Raw payload ───────────────────────────────────────────────────────
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "domain_tags": self.domain_tags,
            "lat": self.lat,
            "lon": self.lon,
            "radius_km": self.radius_km,
            "location_name": self.location_name,
            "severity": self.severity,
            "affected_population": self.affected_population,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status,
            "title": self.title,
            "action_summary": self.action_summary,
            "consensus_flag": self.consensus_flag,
            "allocation": self.allocation,
        }


# ── Agent result containers ───────────────────────────────────────────────

@dataclass
class AgentResult:
    """Base class for all agent outputs."""
    reason: str
    agent_name: str

@dataclass
class DetectionResult(AgentResult):
    accepted: bool = True

@dataclass
class ClassificationResult(AgentResult):
    event_type: str = "unknown"
    domain_tags: list[str] = field(default_factory=list)

@dataclass
class SeverityResult(AgentResult):
    score: int = 1
    raw_score: float = 1.0

@dataclass
class AllocationResult(AgentResult):
    resources: list[str] = field(default_factory=list)
    eta_minutes: int = 0
    depot_lat: float = 0.0
    depot_lon: float = 0.0
    depot_name: str = ""
    depot_org: str = ""
    transport_mode: str = "air"
    # Multi-hub convoy data — list of {hub_name, hub_org, hub_lat, hub_lon, transport, dist_km, eta_minutes, supplies}
    convoys: list[dict] = field(default_factory=list)
    need: dict = field(default_factory=dict)       # AidNeed.to_dict()
    total_committed: dict = field(default_factory=dict)

@dataclass
class CommunicationResult(AgentResult):
    summary: str = ""
    globe_color: str = "#3B8BD4"   # severity colour for the globe marker
    arc_source: tuple[float, float] = (0.0, 0.0)
    arc_dest: tuple[float, float] = (0.0, 0.0)
    # Multi-arc data for globe (one per contributing hub)
    arcs: list[dict] = field(default_factory=list)
