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

  Twitter / X Filtered Stream
    URL  : https://api.twitter.com/2/tweets/search/stream
    Auth : Bearer token (Twitter Developer account)
    Data : NDJSON stream, one tweet JSON per line
    Mode : persistent connection, reconnects on drop
    Keywords: "drone strike" OR "airstrike" OR "shelling" OR "bombing"
              OR "missile strike" OR "ground offensive" lang:en
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

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
            r = requests.get(self.URL, timeout=10)
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
                confidence=0.92,
                raw=p,
            ))
        log.info("USGS: fetched %d events", len(events))
        return events


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
            r = requests.get(self.URL, headers=self.HEADERS, timeout=10)
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

            # NOAA polygons can be complex; use centroid approximation
            lat, lon = 0.0, 0.0
            if coords and geom.get("type") == "Polygon":
                ring = coords[0]
                lat = sum(c[1] for c in ring) / len(ring)
                lon = sum(c[0] for c in ring) / len(ring)

            event_name = p.get("event", "Weather Alert")
            ev_type = self.NOAA_TYPE_MAP.get(event_name, "storm")
            sev_str = p.get("severity", "Moderate")
            urgency = p.get("urgency", "")

            severity = _noaa_severity_to_int(sev_str)
            if urgency == "Immediate":
                severity = min(5, severity + 1)

            events.append(CrisisEvent(
                source="noaa",
                type=ev_type,
                lat=lat,
                lon=lon,
                radius_km=50.0,
                location_name=p.get("areaDesc", ""),
                severity=severity,
                timestamp=_utcnow(),
                title=f"{event_name} — {p.get('areaDesc', '')}",
                confidence=0.90,
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
            r = requests.get(self.URL, headers=self.HEADERS, timeout=15)
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
                confidence=0.88,
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
            r = requests.get(self.URL, timeout=10)
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
                confidence=0.80,
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
                confidence=0.85,
                raw=item,
            ))
        log.info("ACLED: fetched %d events", len(events))
        return events


# ── Twitter / X Filtered Stream ───────────────────────────────────────────

CONFLICT_KEYWORDS = [
    "drone strike", "airstrike", "air strike", "shelling",
    "bombing", "missile strike", "ground offensive", "artillery fire",
    "rocket attack", "mortar attack", "military offensive",
]

CONFLICT_RULES = " OR ".join(f'"{kw}"' for kw in CONFLICT_KEYWORDS) + " lang:en"


class TwitterAdapter:
    """
    Opens a persistent filtered stream connection to Twitter/X API v2.
    Only pulls tweets matching conflict-signal keywords.

    Keywords (server-side filtered, not client-side):
        "drone strike" OR "airstrike" OR "shelling" OR "bombing" OR
        "missile strike" OR "ground offensive" OR "artillery fire" OR
        "rocket attack" OR "mortar attack" lang:en

    Each matching tweet is converted to a CrisisEvent with type=conflict.
    Location is extracted from tweet.geo if present, otherwise the
    adapter attempts a simple place-name extraction from the tweet text
    (first proper noun after a preposition: "in Kyiv", "near Kharkiv").
    """

    STREAM_URL = "https://api.twitter.com/2/tweets/search/stream"
    RULES_URL = "https://api.twitter.com/2/tweets/search/stream/rules"
    PARAMS = "tweet.fields=text,geo,created_at,author_id,lang&expansions=geo.place_id"

    def __init__(self):
        self.bearer = os.getenv("TWITTER_BEARER_TOKEN", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.bearer}"}

    def _setup_rules(self):
        """Remove old rules and install the conflict keyword rule."""
        existing = requests.get(self.RULES_URL, headers=self._headers()).json()
        ids = [r["id"] for r in existing.get("data", [])]
        if ids:
            requests.post(
                self.RULES_URL,
                headers=self._headers(),
                json={"delete": {"ids": ids}},
            )
        requests.post(
            self.RULES_URL,
            headers=self._headers(),
            json={"add": [{"value": CONFLICT_RULES, "tag": "conflict_signals"}]},
        )
        log.info("Twitter: stream rules installed")

    def _extract_location(self, text: str) -> tuple[float, float]:
        """
        Very lightweight heuristic: looks for 'in <Place>' or 'near <Place>'
        and returns (0, 0) if nothing found. In production, swap for a proper
        NER geocoder (spaCy + geopy).
        """
        import re
        m = re.search(r"\b(?:in|near|outside|at)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", text)
        if m:
            log.debug("Twitter: extracted place '%s' (not geocoded)", m.group(1))
        return 0.0, 0.0  # geocoding left as integration step

    def stream(self, callback: Callable[[CrisisEvent], None]):
        if not self.bearer:
            log.warning("Twitter: no bearer token — skipping stream")
            return

        self._setup_rules()
        backoff = 1
        while True:
            try:
                with requests.get(
                    f"{self.STREAM_URL}?{self.PARAMS}",
                    headers=self._headers(),
                    stream=True,
                    timeout=30,
                ) as resp:
                    resp.raise_for_status()
                    backoff = 1
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        import json
                        data = json.loads(line)
                        tweet = data.get("data", {})
                        text = tweet.get("text", "")

                        matched = sum(1 for kw in CONFLICT_KEYWORDS if kw in text.lower())
                        confidence = min(0.4 + matched * 0.08, 0.75)

                        lat, lon = self._extract_location(text)
                        event = CrisisEvent(
                            source="twitter",
                            type="conflict",
                            lat=lat,
                            lon=lon,
                            radius_km=15.0,
                            location_name="",
                            severity=2 + min(matched, 2),
                            timestamp=_utcnow(),
                            title=text[:120],
                            confidence=confidence,
                            raw=tweet,
                        )
                        callback(event)
            except Exception as e:
                log.error("Twitter stream error: %s — retrying in %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
