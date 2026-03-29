"""
Ingest tools — one fetch function per data source.

Each function wraps an existing adapter and returns a JSON string of CrisisEvent dicts.
These tools are called by the 4 parallel LlmAgents in the IngestAgent ParallelAgent.
Each tool also writes its results into ADK session state so the prioritize step can merge them.
"""

from __future__ import annotations

import json
import logging
import os
import sys

# Allow imports from the parent project directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from adapters import USGSAdapter, NOAAAdapter, GDACSAdapter, EONETAdapter
from google.adk.tools.tool_context import ToolContext

log = logging.getLogger("crisisflow.adk.ingest")


def fetch_usgs_earthquakes(tool_context: ToolContext) -> str:
    """
    Fetch recent major earthquakes from USGS. Returns JSON list of crisis events.
    Writes results to state key 'usgs_events'.
    """
    try:
        events = USGSAdapter().fetch()
        data = [e.to_dict() for e in events]
        tool_context.state["usgs_events"] = json.dumps(data)
        log.info("USGS ADK tool: %d events", len(data))
        return f"Fetched {len(data)} earthquake events from USGS."
    except Exception as exc:
        log.error("fetch_usgs_earthquakes failed: %s", exc)
        tool_context.state["usgs_events"] = "[]"
        return f"USGS fetch failed: {exc}"


def fetch_noaa_alerts(tool_context: ToolContext) -> str:
    """
    Fetch active weather alerts from NOAA (hurricanes, tornadoes, floods, storms).
    Writes results to state key 'noaa_events'.
    """
    try:
        events = NOAAAdapter().fetch()
        data = [e.to_dict() for e in events]
        tool_context.state["noaa_events"] = json.dumps(data)
        log.info("NOAA ADK tool: %d events", len(data))
        return f"Fetched {len(data)} weather alerts from NOAA."
    except Exception as exc:
        log.error("fetch_noaa_alerts failed: %s", exc)
        tool_context.state["noaa_events"] = "[]"
        return f"NOAA fetch failed: {exc}"


def fetch_gdacs_disasters(tool_context: ToolContext) -> str:
    """
    Fetch global disaster alerts from GDACS (cyclones, volcanoes, floods, droughts).
    Writes results to state key 'gdacs_events'.
    """
    try:
        events = GDACSAdapter().fetch()
        data = [e.to_dict() for e in events]
        tool_context.state["gdacs_events"] = json.dumps(data)
        log.info("GDACS ADK tool: %d events", len(data))
        return f"Fetched {len(data)} disaster alerts from GDACS."
    except Exception as exc:
        log.error("fetch_gdacs_disasters failed: %s", exc)
        tool_context.state["gdacs_events"] = "[]"
        return f"GDACS fetch failed: {exc}"


def fetch_eonet_events(tool_context: ToolContext) -> str:
    """
    Fetch open natural events from NASA EONET (wildfires, severe storms, icebergs).
    Writes results to state key 'eonet_events'.
    """
    try:
        events = EONETAdapter().fetch()
        data = [e.to_dict() for e in events]
        tool_context.state["eonet_events"] = json.dumps(data)
        log.info("EONET ADK tool: %d events", len(data))
        return f"Fetched {len(data)} natural events from NASA EONET."
    except Exception as exc:
        log.error("fetch_eonet_events failed: %s", exc)
        tool_context.state["eonet_events"] = "[]"
        return f"EONET fetch failed: {exc}"


def prioritize_events(tool_context: ToolContext) -> str:
    """
    Merge all 4 ingest results from state, deduplicate, and select the highest-severity
    crisis event to process next. Writes the chosen event JSON to state['current_event'].
    Returns a summary of what was selected.
    """
    all_events: list[dict] = []
    for key in ("usgs_events", "noaa_events", "gdacs_events", "eonet_events"):
        raw = tool_context.state.get(key, "[]")
        try:
            batch = json.loads(raw) if isinstance(raw, str) else raw
            all_events.extend(batch)
        except Exception:
            pass

    if not all_events:
        tool_context.state["current_event"] = "{}"
        return "No events found across all sources."

    # Deduplicate by title prefix + source
    seen: set[str] = set()
    unique: list[dict] = []
    for e in all_events:
        key = f"{e.get('source','')}:{e.get('title','')[:50]}"
        if key not in seen:
            seen.add(key)
            # Filter out placeholder coordinates
            if not (e.get("lat", 0) == 0.0 and e.get("lon", 0) == 0.0):
                unique.append(e)

    if not unique:
        unique = all_events[:1]

    # Pick highest severity
    best = max(unique, key=lambda e: (e.get("severity", 1), e.get("affected_population", 0)))
    tool_context.state["current_event"] = json.dumps(best)
    tool_context.state["total_ingested"] = len(all_events)

    return (
        f"Selected: [{best.get('source','?').upper()}] {best.get('title','?')} "
        f"— Severity {best.get('severity', '?')}/5 "
        f"(from {len(unique)} unique events across {len(all_events)} total)"
    )
