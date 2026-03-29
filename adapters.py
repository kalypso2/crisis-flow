"""
Ingestion adapters — one per data source.

Each adapter has a single public method:
    fetch() -> list[CrisisEvent]

or for streaming sources:

    stream(callback) -> None   (runs until cancelled)

All adapters normalise their source payload into CrisisEvent before returning.
The raw original payload is preserved in event.raw so agents can access
source-specific fields without the schema needing to know about them.

─────────────────────────────────────────────────────────────────────────────
SOURCE OVERVIEW

  USGS Earthquakes
    URL  : https://earthquake.usgs.gov/fdsnws/event/1/query
    Auth : none (public)
    Data : GeoJSON FeatureCollection, one feature per quake
    Poll : every 30 s
    Key fields: properties.mag, properties.alert, properties.place,
                properties.time, geometry.coordinates [lon, lat, depth]

  NOAA Weather Alerts
    URL  : https://api.weather.gov/alerts/active
    Auth : none (public, User-Agent header required)
    Data : GeoJSON FeatureCollection, one feature per NWS alert
    Poll : every 60 s
    Key fields: properties.event, properties.severity, properties.urgency,
                properties.headline, properties.areaDesc,
                geometry (polygon or point, sometimes null → use areaDesc)

  GDACS
    URL  : https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH
    Auth : none (public)
    Data : JSON array of disaster events
    Poll : every 5 min
    Key fields: eventtype (EQ/FL/TC/VO/WF/DR), alertlevel (Green/Orange/Red),
                latitude, longitude, country, episodealertlevel, name

  NASA EONET
    URL  : https://eonet.gsfc.nasa.gov/api/v3/events
    Auth : none (public)
    Data : JSON, events array with categories and geometry
    Poll : every 60 s
    Key fields: categories[0].id (wildfires/severeStorms/volcanoes/icebergs),
                geometry[0].coordinates [lon, lat], title

  ACLED
    URL  : https://api.acleddata.com/acled/read
    Auth : API key + email (free registration at acleddata.com)
    Data : JSON, data array of conflict events
    Poll : every 5 min
    Key fields: event_type, sub_event_type, country, latitude, longitude,
                fatalities, actor1, actor2, notes, event_date

─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math
import os
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Callable

from schema import CrisisEvent

log = logging.getLogger(__name__)

# ── Shared helpers ────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def _gdacs_alert_to_severity(level: str) -> int:
    return {"green": 2, "orange": 3, "red": 4}.get(level.lower(), 2)

def _usgs_mag_to_severity(mag: float, alert: str | None) -> int:
    if mag >= 7.0:   base = 5
    elif mag >= 6.0: base = 4
    elif mag >= 5.0: base = 3
    else:            base = 2
    boost = {"orange": 1, "red": 2}.get((alert or "").lower(), 0)
    return min(5, base + boost)

def _noaa_severity_to_int(s: str) -> int:
    return {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4,
            "Unknown": 2}.get(s, 2)

def _eonet_category_to_type(cat_id: str) -> str:
    return {
        "wildfires": "wildfire",
        "severeStorms": "storm",
        "volcanoes": "volcano",
        "seaLakeIce": "iceberg",
        "floods": "flood",
        "earthquakes": "earthquake",
    }.get(cat_id, "unknown")


def _request_without_env_proxy(method: str, url: str, **kwargs):
    """
    Issue HTTP requests without inheriting proxy env vars.
    This keeps public-source adapters working in proxied shells.
    """
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, url, **kwargs)

ACLED_TYPE_MAP = {
    "Battles": "conflict",
    "Explosions/Remote violence": "conflict",
    "Violence against civilians": "conflict",
    "Protests": "conflict",
    "Riots": "conflict",
    "Strategic developments": "conflict",
}


# ── USGS Earthquakes ──────────────────────────────────────────────────────

class USGSAdapter:
    """
    Downloads the 50 most recent earthquakes of magnitude >= 4.0.
    Returns one CrisisEvent per quake.
    """
    URL = (
        "https://earthquake.usgs.gov/fdsnws/event/1/query"
        "?format=geojson&minmagnitude=4.0&limit=50&orderby=time"
    )

    def fetch(self) -> list[CrisisEvent]:
        try:
            r = _request_without_env_proxy("GET", self.URL, timeout=10)
            r.raise_for_status()
            features = r.json().get("features", [])
        except Exception as e:
            log.error("USGS fetch failed: %s", e)
            return []

        events = []
        for f in features:
            p = f["properties"]
            coords = f["geometry"]["coordinates"]  # [lon, lat, depth]
            mag = p.get("mag") or 0
            alert = p.get("alert")
            events.append(CrisisEvent(
                source="usgs",
                type="earthquake",
                lat=coords[1],
                lon=coords[0],
                radius_km=max(10, mag * 15),
                location_name=p.get("place", ""),
                severity=_usgs_mag_to_severity(mag, alert),
                timestamp=_ms_to_dt(p["time"]),
                title=f"M{mag} earthquake — {p.get('place', 'unknown')}",
                raw=p,
            ))
        log.info("USGS: fetched %d events", len(events))
        return events


# ── NOAA US state centroids (fallback for null-geometry alerts) ────────────
# Sourced from geographic centre of each state.
_US_STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.80,  -86.79), "AK": (64.20, -153.37), "AZ": (34.30, -111.09),
    "AR": (34.75,  -92.13), "CA": (37.15, -119.54), "CO": (39.00, -105.55),
    "CT": (41.60,  -72.70), "DE": (39.00,  -75.50), "FL": (27.99,  -81.76),
    "GA": (32.64,  -83.44), "HI": (20.24, -156.36), "ID": (44.27, -114.61),
    "IL": (40.35,  -88.99), "IN": (39.85,  -86.26), "IA": (42.01,  -93.21),
    "KS": (38.53,  -96.73), "KY": (37.64,  -84.87), "LA": (30.39,  -92.33),
    "ME": (44.69,  -69.38), "MD": (39.06,  -76.80), "MA": (42.26,  -71.81),
    "MI": (44.18,  -84.47), "MN": (46.39,  -94.63), "MS": (32.74,  -89.67),
    "MO": (38.46,  -92.29), "MT": (46.88, -110.36), "NE": (41.49,  -99.90),
    "NV": (39.33, -116.62), "NH": (43.68,  -71.58), "NJ": (40.06,  -74.41),
    "NM": (34.31, -106.02), "NY": (42.95,  -75.52), "NC": (35.54,  -79.39),
    "ND": (47.45, -100.47), "OH": (40.29,  -82.79), "OK": (35.59,  -97.49),
    "OR": (43.94, -120.56), "PA": (40.87,  -77.79), "RI": (41.68,  -71.56),
    "SC": (33.90,  -80.90), "SD": (44.44,  -99.88), "TN": (35.85,  -86.35),
    "TX": (31.47,  -99.33), "UT": (39.32, -111.09), "VT": (44.07,  -72.67),
    "VA": (37.51,  -78.86), "WA": (47.38, -120.45), "WV": (38.64,  -80.62),
    "WI": (44.27,  -89.62), "WY": (42.96, -107.55), "DC": (38.91,  -77.01),
    "PR": (18.22,  -66.59), "GU": (13.44,  144.79), "VI": (17.73,  -64.73),
}

def _noaa_state_centroid(area_desc: str) -> tuple[float, float] | None:
    """
    Extract a US state abbreviation from NOAA's areaDesc and return its centroid.
    areaDesc examples: "Central Oklahoma", "Western TX; Eastern NM", "Lake Erie"
    """
    tokens = area_desc.upper().replace(",", " ").replace(";", " ").split()
    for token in tokens:
        if token in _US_STATE_CENTROIDS:
            return _US_STATE_CENTROIDS[token]
    return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _split_multipolygon(
    mp_coords: list,
    max_splits: int = 5,
    min_dist_km: float = 200.0,
) -> list[tuple[float, float]]:
    """
    Given MultiPolygon coordinates, return up to max_splits centroids that are
    all at least min_dist_km apart from each other (greedy selection in order).
    """
    selected: list[tuple[float, float]] = []
    for polygon in mp_coords:
        if len(selected) >= max_splits:
            break
        ring = polygon[0]  # outer ring
        if not ring:
            continue
        lat = sum(c[1] for c in ring) / len(ring)
        lon = sum(c[0] for c in ring) / len(ring)
        if all(_haversine_km(lat, lon, s_lat, s_lon) >= min_dist_km
               for s_lat, s_lon in selected):
            selected.append((lat, lon))
    return selected


# ── NOAA Weather Alerts ───────────────────────────────────────────────────

class NOAAAdapter:
    """
    Downloads all currently active NWS weather alerts (US-focused).
    Each alert is one CrisisEvent.
    """
    URL = "https://api.weather.gov/alerts/active?status=actual&message_type=alert"
    HEADERS = {"User-Agent": "CrisisFlow/1.0 (hackathon@example.com)"}

    NOAA_TYPE_MAP = {
        "Tornado Warning": "storm",
        "Tornado Watch": "storm",
        "Hurricane Warning": "cyclone",
        "Hurricane Watch": "cyclone",
        "Tropical Storm Warning": "cyclone",
        "Flash Flood Warning": "flood",
        "Flood Warning": "flood",
        "Severe Thunderstorm Warning": "storm",
        "Winter Storm Warning": "storm",
        "Blizzard Warning": "storm",
        "Dust Storm Warning": "storm",
    }

    def fetch(self) -> list[CrisisEvent]:
        try:
            r = _request_without_env_proxy("GET", self.URL, headers=self.HEADERS, timeout=10)
            r.raise_for_status()
            features = r.json().get("features", [])
        except Exception as e:
            log.error("NOAA fetch failed: %s", e)
            return []

        events = []
        for f in features:
            p = f["properties"]
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []

            event_name = p.get("event", "Weather Alert")
            ev_type = self.NOAA_TYPE_MAP.get(event_name, "storm")
            sev_str = p.get("severity", "Moderate")
            urgency = p.get("urgency", "")
            severity = _noaa_severity_to_int(sev_str)
            if urgency == "Immediate":
                severity = min(5, severity + 1)
            area_desc = p.get("areaDesc", "")

            # ── Coordinate extraction ─────────────────────────────────────
            geom_type = geom.get("type")
            lat, lon = 0.0, 0.0
            split_centroids: list[tuple[float, float]] = []

            if coords and geom_type == "Polygon":
                ring = coords[0]
                lat = sum(c[1] for c in ring) / len(ring)
                lon = sum(c[0] for c in ring) / len(ring)

            elif coords and geom_type == "MultiPolygon":
                candidates = _split_multipolygon(coords)  # ≤5, ≥200 km apart
                if len(candidates) > 1:
                    split_centroids = candidates  # emit one event per centroid
                elif candidates:
                    lat, lon = candidates[0]  # all polygons are close — single point

            if not split_centroids and lat == 0.0 and lon == 0.0:
                # Null / unrecognised geometry — fall back to state centroid
                centroid = _noaa_state_centroid(area_desc)
                if centroid:
                    lat, lon = centroid

            # ── Event creation ────────────────────────────────────────────
            if split_centroids:
                total = len(split_centroids)
                for idx, (p_lat, p_lon) in enumerate(split_centroids, start=1):
                    events.append(CrisisEvent(
                        source="noaa",
                        type=ev_type,
                        lat=p_lat,
                        lon=p_lon,
                        radius_km=50.0,
                        location_name=area_desc,
                        severity=severity,
                        timestamp=_utcnow(),
                        title=f"{event_name} — {area_desc} ({idx} of {total})",
                        raw=p,
                    ))
            else:
                events.append(CrisisEvent(
                    source="noaa",
                    type=ev_type,
                    lat=lat,
                    lon=lon,
                    radius_km=50.0,
                    location_name=area_desc,
                    severity=severity,
                    timestamp=_utcnow(),
                    title=f"{event_name} — {area_desc}",
                    raw=p,
                ))
        log.info("NOAA: fetched %d alerts", len(events))
        return events


# ── GDACS ─────────────────────────────────────────────────────────────────

class GDACSAdapter:
    """
    Downloads the GDACS global disaster alert list.
    Covers: earthquakes (EQ), floods (FL), tropical cyclones (TC),
            volcanoes (VO), wildfires (WF), droughts (DR).
    """
    URL = (
        "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
        "?alertlevel=&eventtype=&fromdate=&todate=&country=&limit=50"
    )
    HEADERS = {"Accept": "application/json"}

    GDACS_TYPE_MAP = {
        "EQ": "earthquake", "FL": "flood", "TC": "cyclone",
        "VO": "volcano",    "WF": "wildfire", "DR": "drought",
    }

    def fetch(self) -> list[CrisisEvent]:
        try:
            r = _request_without_env_proxy("GET", self.URL, headers=self.HEADERS, timeout=15)
            r.raise_for_status()
            items = r.json().get("features", [])
        except Exception as e:
            log.error("GDACS fetch failed: %s", e)
            return []

        events = []
        for item in items:
            p = item.get("properties", {})
            geom = item.get("geometry", {})
            coords = geom.get("coordinates", [0.0, 0.0])
            ev_type = self.GDACS_TYPE_MAP.get(p.get("eventtype", ""), "unknown")
            alert = p.get("alertlevel", "green")

            events.append(CrisisEvent(
                source="gdacs",
                type=ev_type,
                lat=coords[1] if len(coords) > 1 else 0.0,
                lon=coords[0],
                radius_km=p.get("affectedpopulation", 50),
                location_name=p.get("country", ""),
                severity=_gdacs_alert_to_severity(alert),
                timestamp=_utcnow(),
                title=p.get("name", f"GDACS {ev_type}"),
                raw=p,
            ))
        log.info("GDACS: fetched %d events", len(events))
        return events


# ── NASA EONET ────────────────────────────────────────────────────────────

class EONETAdapter:
    """
    Downloads open natural events from NASA EONET.
    Covers: wildfires, severe storms, icebergs, volcanic activity.
    """
    URL = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=50"

    def fetch(self) -> list[CrisisEvent]:
        try:
            r = _request_without_env_proxy("GET", self.URL, timeout=10)
            r.raise_for_status()
            items = r.json().get("events", [])
        except Exception as e:
            log.error("EONET fetch failed: %s", e)
            return []

        events = []
        for item in items:
            cats = item.get("categories", [])
            cat_id = cats[0]["id"] if cats else "unknown"
            geometries = item.get("geometry", [])
            if not geometries:
                continue
            geom = geometries[-1]  # most recent geometry
            coords = geom.get("coordinates", [0.0, 0.0])
            lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (0.0, 0.0)

            ev_type = _eonet_category_to_type(cat_id)
            events.append(CrisisEvent(
                source="eonet",
                type=ev_type,
                lat=lat,
                lon=lon,
                radius_km=30.0,
                location_name=item.get("title", ""),
                severity=3,  # EONET has no alert levels; severity agent refines
                timestamp=_utcnow(),
                title=item.get("title", f"EONET {ev_type}"),
                raw=item,
            ))
        log.info("EONET: fetched %d events", len(events))
        return events


# ── ACLED Conflicts ───────────────────────────────────────────────────────

class ACLEDAdapter:
    """
    Downloads recent armed conflict events from ACLED.
    Requires free API key from acleddata.com.
    Covers: battles, explosions/remote violence, drone strikes, civilian targeting.
    """

    def __init__(self):
        self.api_key = os.getenv("ACLED_API_KEY", "")
        self.email = os.getenv("ACLED_EMAIL", "")

    def fetch(self) -> list[CrisisEvent]:
        if not self.api_key:
            log.warning("ACLED: no API key — skipping")
            return []

        today = datetime.utcnow().strftime("%Y-%m-%d")
        url = (
            f"https://api.acleddata.com/acled/read"
            f"?key={self.api_key}&email={self.email}"
            f"&event_date={today}&event_date_where=BETWEEN"
            f"&limit=50&fields=event_type|sub_event_type|country|"
            f"latitude|longitude|fatalities|actor1|notes|event_date"
        )

        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            items = r.json().get("data", [])
        except Exception as e:
            log.error("ACLED fetch failed: %s", e)
            return []

        events = []
        for item in items:
            fatalities = int(item.get("fatalities", 0) or 0)
            if fatalities == 0:        sev = 2
            elif fatalities <= 10:     sev = 3
            elif fatalities <= 50:     sev = 4
            else:                      sev = 5

            lat = float(item.get("latitude") or 0)
            lon = float(item.get("longitude") or 0)
            ev_type = ACLED_TYPE_MAP.get(item.get("event_type", ""), "conflict")

            events.append(CrisisEvent(
                source="acled",
                type=ev_type,
                lat=lat,
                lon=lon,
                radius_km=20.0,
                location_name=item.get("country", ""),
                severity=sev,
                timestamp=_utcnow(),
                title=f"{item.get('sub_event_type','Conflict event')} — {item.get('country','')}",
                raw=item,
            ))
        log.info("ACLED: fetched %d events", len(events))
        return events

