"""
CrisisFlow MCP Server — Snowflake + Open-Meteo data grounding via MCP.

Exposes disaster event data from Snowflake weather tables and live weather
context from Open-Meteo as MCP tools that ADK agents consume via
``MCPToolset(StdioServerParameters(...))``.

Run standalone:  python -m crisisflow_mcp.server
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

log = logging.getLogger("crisisflow.mcp")

mcp = FastMCP("CrisisFlow Disaster Intelligence")


def _inventory_api_base() -> str:
    return os.environ.get("CRISISFLOW_INVENTORY_API_BASE", "").strip().rstrip("/")


def _flask_inventory_rows() -> list | None:
    """When CRISISFLOW_INVENTORY_API_BASE is set, mirror Flask /inventory (live commits)."""
    base = _inventory_api_base()
    if not base:
        return None
    try:
        import urllib.request

        with urllib.request.urlopen(f"{base}/inventory", timeout=90) as r:
            data = json.loads(r.read().decode())
        return data if isinstance(data, list) else None
    except Exception as exc:
        log.warning("CRISISFLOW_INVENTORY_API_BASE fetch failed: %s", exc)
        return None

MARKERS_PATH = ROOT / "data" / "acled_globe_markers.json"
FLAT_PATH = ROOT / "data" / "acled_90d_violence_flat.json"

_markers_cache: list[dict] | None = None

TYPE_TO_TABLE = {
    "earthquake": "EARTHQUAKES",
    "flood": "FLOODS",
    "storm": "STORMS",
    "cyclone": "CYCLONES",
    "wildfire": "WILDFIRES",
    "volcano": "VOLCANOES",
    "drought": "DROUGHTS",
    "iceberg": "ICEBERGS",
}


def _load_markers() -> list[dict]:
    global _markers_cache
    if _markers_cache is not None:
        return _markers_cache
    refined = ROOT / "data" / "acled_globe_markers_refined.json"
    path = refined if refined.exists() else MARKERS_PATH
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        _markers_cache = json.load(f)
    return _markers_cache


def _get_snowflake_conn():
    import snowflake_store
    return snowflake_store._get_conn()


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = la2 - la1, lo2 - lo1
    a = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# WMO weather code descriptions
_WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


# ── Conflict zone tools (kept for compatibility) ──────────────────────────


@mcp.tool
def query_conflict_zones(region: str) -> str:
    """Query conflict zones in a specific ACLED region.

    Args:
        region: ACLED region name, e.g. "Middle East", "Eastern Africa".
    """
    markers = _load_markers()
    zones = [m for m in markers if m.get("region", "") == region]
    if not zones:
        return json.dumps({"error": f"No zones for region '{region}'", "zones": []})
    slim = [{
        "zone_id": z.get("id", ""), "country": z.get("country", ""),
        "admin1": z.get("admin1", ""), "title": z.get("title", ""),
        "severity": z.get("severity", 1), "total_events": z.get("total_events", 0),
        "total_fatalities": z.get("total_fatalities", 0),
        "week_first": z.get("week_first", ""), "week_last": z.get("week_last", ""),
        "primary_event_type": z.get("primary_event_type", ""),
    } for z in zones]
    return json.dumps({"region": region, "zone_count": len(slim), "zones": slim})


@mcp.tool
def list_regions() -> str:
    """List all available ACLED regions and how many conflict zones each has."""
    markers = _load_markers()
    regions: dict[str, int] = {}
    for m in markers:
        r = m.get("region", "Unknown")
        regions[r] = regions.get(r, 0) + 1
    return json.dumps({"regions": regions, "total_zones": len(markers)})


# ── Disaster event tools (weather/hazard data from Snowflake) ─────────────


@mcp.tool
def query_disaster_events(
    event_type: str, limit: int = 10, unenriched_only: bool = False
) -> str:
    """Query recent disaster events from Snowflake by type.

    Returns the highest-severity events first. Each row includes id, title,
    lat, lon, severity, timestamp, source, location_name, domain_tags, and
    action_summary.

    Args:
        event_type: One of: earthquake, flood, storm, cyclone, wildfire,
                    volcano, drought, iceberg.
        limit: Max events to return (default 10, max 20).
        unenriched_only: If True, only rows where enrichment is not complete
            (pipeline queue). If False (default), return top events regardless
            of enrichment so IDs always match Snowflake rows used by the UI.
    """
    table = TYPE_TO_TABLE.get(event_type)
    if not table:
        return json.dumps({"error": f"Unknown event_type '{event_type}'",
                           "valid_types": list(TYPE_TO_TABLE.keys())})
    limit = min(int(limit), 20)
    try:
        import snowflake.connector
        conn = _get_snowflake_conn()
        cur = conn.cursor(snowflake.connector.DictCursor)
        where = (
            "WHERE ENRICHMENT_STATUS IS NULL OR ENRICHMENT_STATUS != 'complete'"
            if unenriched_only
            else ""
        )
        cur.execute(f"""
            SELECT ID, TITLE, LAT, LON, SEVERITY, TIMESTAMP, SOURCE,
                   LOCATION_NAME, DOMAIN_TAGS, ACTION_SUMMARY,
                   AFFECTED_POPULATION, RADIUS_KM
            FROM {table}
            {where}
            ORDER BY SEVERITY DESC, TIMESTAMP DESC
            LIMIT %s
        """, (limit,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if isinstance(d.get("DOMAIN_TAGS"), str):
                try:
                    d["DOMAIN_TAGS"] = json.loads(d["DOMAIN_TAGS"])
                except (json.JSONDecodeError, TypeError):
                    pass
            rows.append({
                "id": d["ID"], "title": d["TITLE"],
                "lat": d["LAT"], "lon": d["LON"],
                "severity": d["SEVERITY"],
                "timestamp": str(d["TIMESTAMP"]),
                "source": d["SOURCE"],
                "location_name": d.get("LOCATION_NAME", ""),
                "domain_tags": d.get("DOMAIN_TAGS", []),
                "action_summary": d.get("ACTION_SUMMARY", ""),
                "affected_population": d.get("AFFECTED_POPULATION", 0),
                "radius_km": d.get("RADIUS_KM", 0),
            })
        return json.dumps({
            "event_type": event_type, "count": len(rows), "events": rows
        })
    except Exception as exc:
        log.error("query_disaster_events failed: %s", exc)
        return json.dumps({"error": str(exc), "events": []})


@mcp.tool
def get_nearby_disasters(lat: float, lon: float,
                         radius_km: float = 500,
                         hours_back: int = 72) -> str:
    """Find other disaster events near a location to detect compound risks.

    Searches ALL disaster tables for events within radius_km of the given
    coordinates that occurred in the last hours_back hours.

    Args:
        lat: Latitude of the reference point.
        lon: Longitude of the reference point.
        radius_km: Search radius in km (default 500).
        hours_back: Look back this many hours (default 72).
    """
    nearby: list[dict] = []
    try:
        import snowflake.connector
        conn = _get_snowflake_conn()
        cur = conn.cursor(snowflake.connector.DictCursor)
        for etype, table in TYPE_TO_TABLE.items():
            cur.execute(f"""
                SELECT ID, TITLE, LAT, LON, SEVERITY, TIMESTAMP, SOURCE
                FROM {table}
                WHERE TIMESTAMP > DATEADD(hour, -%s, CURRENT_TIMESTAMP())
                ORDER BY SEVERITY DESC
                LIMIT 50
            """, (hours_back,))
            for r in cur.fetchall():
                d = dict(r)
                dist = _haversine_km(lat, lon, float(d["LAT"]), float(d["LON"]))
                if dist <= radius_km:
                    nearby.append({
                        "event_type": etype,
                        "id": d["ID"], "title": d["TITLE"],
                        "lat": d["LAT"], "lon": d["LON"],
                        "severity": d["SEVERITY"],
                        "timestamp": str(d["TIMESTAMP"]),
                        "distance_km": round(dist, 1),
                    })
        nearby.sort(key=lambda x: x["distance_km"])
        return json.dumps({
            "center": {"lat": lat, "lon": lon},
            "radius_km": radius_km,
            "hours_back": hours_back,
            "count": len(nearby),
            "nearby_events": nearby[:20],
        })
    except Exception as exc:
        log.error("get_nearby_disasters failed: %s", exc)
        return json.dumps({"error": str(exc), "nearby_events": []})


# ── Open-Meteo weather tools ─────────────────────────────────────────────


@mcp.tool
def get_weather_forecast(lat: float, lon: float) -> str:
    """Get current weather conditions and 7-day forecast for a location.

    Uses the free Open-Meteo API (no auth required). Returns current
    temperature, wind speed, precipitation, weather description, and
    a daily forecast array.

    Args:
        lat: Latitude of the location.
        lon: Longitude of the location.
    """
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation,weather_code",
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max"),
            "timezone": "auto",
            "forecast_days": 7,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()

        current = data.get("current", {})
        wcode = current.get("weather_code", -1)
        current["weather_description"] = _WMO_CODES.get(wcode, f"Code {wcode}")

        daily = data.get("daily", {})
        forecast = []
        dates = daily.get("time", [])
        for i, date in enumerate(dates):
            dc = daily.get("weather_code", [None] * len(dates))[i]
            forecast.append({
                "date": date,
                "weather": _WMO_CODES.get(dc, f"Code {dc}") if dc is not None else "Unknown",
                "temp_max": daily.get("temperature_2m_max", [None] * len(dates))[i],
                "temp_min": daily.get("temperature_2m_min", [None] * len(dates))[i],
                "precipitation_mm": daily.get("precipitation_sum", [None] * len(dates))[i],
                "wind_max_kmh": daily.get("wind_speed_10m_max", [None] * len(dates))[i],
                "wind_gusts_kmh": daily.get("wind_gusts_10m_max", [None] * len(dates))[i],
            })

        return json.dumps({
            "location": {"lat": lat, "lon": lon},
            "timezone": data.get("timezone", ""),
            "current": current,
            "forecast_7day": forecast,
        })
    except Exception as exc:
        log.error("get_weather_forecast failed: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.tool
def get_weather_history(lat: float, lon: float,
                        start_date: str, end_date: str) -> str:
    """Get historical weather data for a location and date range.

    Uses Open-Meteo Archive API. Useful for understanding weather conditions
    leading up to and during a disaster event.

    Args:
        lat: Latitude of the location.
        lon: Longitude of the location.
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
    """
    try:
        r = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
            "latitude": lat, "longitude": lon,
            "start_date": start_date, "end_date": end_date,
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max"),
            "timezone": "auto",
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        days = []
        dates = daily.get("time", [])
        for i, date in enumerate(dates):
            dc = daily.get("weather_code", [None] * len(dates))[i]
            days.append({
                "date": date,
                "weather": _WMO_CODES.get(dc, f"Code {dc}") if dc is not None else "Unknown",
                "temp_max": daily.get("temperature_2m_max", [None] * len(dates))[i],
                "temp_min": daily.get("temperature_2m_min", [None] * len(dates))[i],
                "precipitation_mm": daily.get("precipitation_sum", [None] * len(dates))[i],
                "wind_max_kmh": daily.get("wind_speed_10m_max", [None] * len(dates))[i],
                "wind_gusts_kmh": daily.get("wind_gusts_10m_max", [None] * len(dates))[i],
            })
        return json.dumps({
            "location": {"lat": lat, "lon": lon},
            "period": {"start": start_date, "end": end_date},
            "days": days,
        })
    except Exception as exc:
        log.error("get_weather_history failed: %s", exc)
        return json.dumps({"error": str(exc)})


# ── Aid distribution tools ────────────────────────────────────────────────


@mcp.tool
def get_depot_inventory() -> str:
    """Return current UNHRD pre-positioned aid stock levels at all global depots.

    Reports shelter kits, food rations, medical kits, water kits, and vehicles
    at each of the 6 UNHRD hubs.  Levels deplete as events are responded to
    and replenish on a weekly cycle (Baseline figures from UNHRD Annual Reports).
    """
    try:
        import depot_inventory as inv

        rows = _flask_inventory_rows()
        if rows is not None:
            baselines = inv.get_all_baselines()
            result = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                hub_name = row.get("hub_name")
                if not hub_name:
                    continue
                stock = row.get("stock") or {}
                base = row.get("baseline") or baselines.get(hub_name, {})
                pct = {}
                for item, qty in stock.items():
                    b = base.get(item, 0)
                    try:
                        qn = float(qty)
                    except (TypeError, ValueError):
                        qn = 0
                    pct[item] = round(qn / b * 100, 1) if b > 0 else 0
                result[hub_name] = {
                    "current_stock": stock,
                    "baseline": base,
                    "fill_pct": pct,
                    "status": row.get("stock_level") or "unknown",
                }
            return json.dumps({"depots": result, "count": len(result)})

        inventory = inv.get_inventory()
        baselines = inv.get_all_baselines()
        result = {}
        for hub_name, stock in inventory.items():
            base = baselines.get(hub_name, {})
            pct = {}
            for item, qty in stock.items():
                b = base.get(item, 0)
                pct[item] = round(qty / b * 100, 1) if b > 0 else 0
            result[hub_name] = {
                "current_stock": stock,
                "baseline": base,
                "fill_pct": pct,
                "status": inv.stock_level(hub_name),
            }
        return json.dumps({"depots": result, "count": len(result)})
    except Exception as exc:
        log.error("get_depot_inventory failed: %s", exc)
        return json.dumps({"error": str(exc), "depots": {}})


@mcp.tool
def get_nearest_depots(lat: float, lon: float, top_n: int = 3) -> str:
    """Find the nearest UNHRD aid depots to a disaster location.

    Returns up to top_n depots sorted by distance, with air ETA estimates,
    current stock status, and specializations for the event type at that
    location.

    Args:
        lat: Latitude of the disaster event.
        lon: Longitude of the disaster event.
        top_n: Number of nearest depots to return (default 3, max 6).
    """
    try:
        import hub_agents
        import depot_inventory as inv

        top_n = min(int(top_n), 6)
        rows = _flask_inventory_rows()
        if rows is not None:
            inventory = {
                r["hub_name"]: r.get("stock") or {}
                for r in rows
                if isinstance(r, dict) and r.get("hub_name")
            }
            level_by_hub = {
                r["hub_name"]: r.get("stock_level") or "unknown"
                for r in rows
                if isinstance(r, dict) and r.get("hub_name")
            }
        else:
            inventory = inv.get_inventory()
            level_by_hub = {}

        hubs_with_dist = []
        for hub in hub_agents.HUBS:
            dist_km = _haversine_km(lat, lon, hub["lat"], hub["lon"])
            # Air ETA: assume 800 km/h cruise + 2h ground/loading
            eta_h = round(dist_km / 800 + 2, 1)
            hname = hub["name"]
            stock_status = level_by_hub.get(hname) if level_by_hub else inv.stock_level(hname)
            current = inventory.get(hname, {})
            hubs_with_dist.append({
                "name": hub["name"],
                "org": hub["org"],
                "lat": hub["lat"],
                "lon": hub["lon"],
                "distance_km": round(dist_km, 0),
                "air_eta_hours": eta_h,
                "specializations": hub.get("specializations", []),
                "stock_status": stock_status,
                "shelter_kits": current.get("shelter_kits", 0),
                "food_rations": current.get("food_rations", 0),
                "medical_kits": current.get("medical_kits", 0),
                "water_kits": current.get("water_kits", 0),
                "vehicles": current.get("vehicles", 0),
            })

        hubs_with_dist.sort(key=lambda h: h["distance_km"])
        return json.dumps({
            "event_location": {"lat": lat, "lon": lon},
            "nearest_depots": hubs_with_dist[:top_n],
        })
    except Exception as exc:
        log.error("get_nearest_depots failed: %s", exc)
        return json.dumps({"error": str(exc), "nearest_depots": []})


@mcp.tool
def calculate_sphere_needs(
    event_type: str,
    severity: int,
    affected_population: int,
) -> str:
    """Calculate Sphere-standard aid requirements for a disaster event.

    Uses the Sphere Handbook 2018 + WFP/WHO/UNICEF WASH Cluster standards to
    compute the exact quantities of each aid type needed.

    Args:
        event_type: Type of disaster (earthquake, flood, cyclone, wildfire,
                    volcano, drought, storm, iceberg).
        severity: Severity level 1-5 (1=minimal, 5=catastrophic).
        affected_population: Estimated number of people affected.
    """
    try:
        from need_calculator import calculate
        need = calculate(event_type, severity, affected_population)
        return json.dumps({
            "event_type": event_type,
            "severity": severity,
            "affected_population": affected_population,
            "sphere_requirements": need.to_dict(),
            "standards_reference": "Sphere Handbook 2018 + WFP/WHO/UNICEF WASH Cluster",
        })
    except Exception as exc:
        log.error("calculate_sphere_needs failed: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.tool
def get_ocha_funding(country_name: str, year: int = 2024) -> str:
    """Fetch real humanitarian aid funding flows for a country from OCHA FTS.

    Queries the OCHA Financial Tracking Service (FTS) — the authoritative UN
    database tracking who funded what for each humanitarian emergency.  Free,
    no authentication required.

    Args:
        country_name: Country name (e.g. "Myanmar", "Haiti", "Philippines").
        year: Year to query (default 2024). Use current year for recent flows.
    """
    try:
        # Step 1: resolve country name to FTS location ID
        loc_resp = requests.get(
            "https://api.hpc.tools/v1/public/location",
            timeout=10,
        )
        loc_resp.raise_for_status()
        locations = loc_resp.json()
        if isinstance(locations, dict):
            locations = locations.get("data", [])

        location_id = None
        matched_name = None
        search_lower = country_name.lower()
        for loc in locations:
            if loc.get("name", "").lower() == search_lower:
                location_id = loc["id"]
                matched_name = loc["name"]
                break
        # Fuzzy fallback: substring match
        if not location_id:
            for loc in locations:
                if search_lower in loc.get("name", "").lower():
                    location_id = loc["id"]
                    matched_name = loc["name"]
                    break

        if not location_id:
            return json.dumps({
                "error": f"Country '{country_name}' not found in OCHA FTS",
                "flows": [],
            })

        # Step 2: fetch funding flows for this location
        flow_resp = requests.get(
            "https://api.hpc.tools/v1/public/fts/flow",
            params={"locationId": location_id, "limit": 20},
            timeout=15,
        )
        flow_resp.raise_for_status()
        flows_raw = flow_resp.json().get("data", {}).get("flows", [])

        total_usd = 0
        flows_out = []
        for f in flows_raw:
            amount = f.get("amountUSD", 0) or 0
            total_usd += amount
            orgs = f.get("organizations", [])
            donors = [o["name"] for o in orgs if o.get("direction") == "source"]
            recipients = [o["name"] for o in orgs if o.get("direction") == "destination"]
            flows_out.append({
                "id": f.get("id"),
                "amount_usd": amount,
                "date": str(f.get("date", ""))[:10],
                "description": f.get("description", "")[:120],
                "donors": donors[:3],
                "recipients": recipients[:3],
                "flow_type": f.get("flowType", ""),
            })

        return json.dumps({
            "country": matched_name,
            "location_id": location_id,
            "total_flows": len(flows_out),
            "total_usd": total_usd,
            "flows": flows_out,
            "source": "OCHA Financial Tracking Service (fts.unocha.org)",
        })
    except Exception as exc:
        log.error("get_ocha_funding failed: %s", exc)
        return json.dumps({"error": str(exc), "flows": []})


if __name__ == "__main__":
    mcp.run()
