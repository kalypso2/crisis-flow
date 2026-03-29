"""
CrisisFlow Logistics Specialist — A2A Agent

A standalone agent that exposes the UNHRD hub allocation logic as an A2A service.
Other agents call this via the A2A protocol rather than importing the module directly,
demonstrating multi-agent interoperability (a key ADK competition requirement).

To run as an A2A service:
  cd crisis-flow
  adk api_server adk/logistics_agent/agent.py --port 8002

The main pipeline's allocation_agent calls:
  POST http://localhost:8002/run
  {"message": "Allocate aid for a magnitude 6.8 earthquake in Nepal..."}

Agent card served at: http://localhost:8002/.well-known/agent.json
"""

from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext

log = logging.getLogger("crisisflow.logistics")

MODEL = "gemini-2.5-flash"


# ── Tools available to the logistics specialist ────────────────────────────

def list_available_hubs(event_type: str, tool_context: ToolContext) -> str:
    """
    List all UNHRD humanitarian depots with their current inventory levels
    and specializations for the given disaster type.
    """
    try:
        import depot_inventory as inv
        from hub_agents import HUBS

        lines = []
        for hub in HUBS:
            name = hub["name"]
            specs = hub.get("specializations", [])
            stock = inv.get_inventory(name) or {}
            is_specialist = event_type.lower() in [s.lower() for s in specs]
            specialist_tag = " ★SPECIALIST" if is_specialist else ""
            vehicles     = stock.get("field_vehicles", 0)
            med_kits     = stock.get("medical_kits", 0)
            food_rations = stock.get("food_rations", 0)
            lines.append(
                f"{name}{specialist_tag}: vehicles={vehicles} "
                f"medical_kits={med_kits} food_rations={food_rations} "
                f"lat={hub['lat']} lon={hub['lon']}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Hub list error: {exc}"


def calculate_aid_need(event_type: str, severity: int, affected_population: int) -> str:
    """
    Calculate the total humanitarian aid needed for a disaster event.
    Returns JSON with quantities of food_rations, medical_kits, shelter_units, field_vehicles needed.
    """
    try:
        from need_calculator import calculate
        need = calculate(event_type, severity, affected_population)
        return json.dumps(need.to_dict())
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def commit_hub_allocation(
    hub_name: str,
    event_type: str,
    severity: int,
    event_lat: float,
    event_lon: float,
    tool_context: ToolContext,
) -> str:
    """
    Commit a specific UNHRD hub to dispatch aid to the event location.
    Deducts from the hub's live inventory and calculates transport ETA.
    Returns JSON with convoy details including transport mode, distance, and ETA.
    """
    try:
        from hub_agents import HubAgent, HUBS
        hub_meta = next((h for h in HUBS if h["name"] == hub_name), None)
        if not hub_meta:
            return json.dumps({"error": f"Unknown hub: {hub_name}"})

        hub_agent = HubAgent(
            name=hub_meta["name"],
            org=hub_meta["org"],
            lat=hub_meta["lat"],
            lon=hub_meta["lon"],
            specializations=hub_meta.get("specializations", []),
        )

        try:
            from claude_manager import ClaudeManager
            claude = ClaudeManager()
        except Exception:
            claude = None

        from need_calculator import calculate
        need = calculate(event_type, severity, 100_000)

        bid = hub_agent.bid(
            event_type=event_type,
            severity=severity,
            need=need,
            event_lat=event_lat,
            event_lon=event_lon,
        )

        if bid is None:
            return json.dumps({"error": f"{hub_name} cannot fulfil this request"})

        # Commit the inventory
        import depot_inventory as inv
        inv.commit(hub_name, bid.committed)

        return json.dumps({
            "hub_name":    hub_name,
            "hub_lat":     hub_meta["lat"],
            "hub_lon":     hub_meta["lon"],
            "hub_org":     hub_meta["org"],
            "transport":   bid.transport,
            "dist_km":     round(bid.dist_km, 1),
            "eta_minutes": bid.eta_minutes,
            "supplies":    bid.committed,
        })
    except Exception as exc:
        log.error("commit_hub_allocation failed: %s", exc)
        return json.dumps({"error": str(exc)})


# ── Logistics Specialist Agent ─────────────────────────────────────────────

root_agent = LlmAgent(
    name="logistics_specialist",
    model=MODEL,
    instruction="""You are the CrisisFlow Logistics Specialist — an expert in humanitarian
supply chain and UN depot management.

When asked to allocate aid for a crisis:
1. Call list_available_hubs with the disaster type to see current depot inventory
2. Call calculate_aid_need to determine total requirements
3. Select the 1-3 best hubs based on: proximity to event, specialization match, stock levels
4. Call commit_hub_allocation for each selected hub to reserve and dispatch supplies
5. Return a JSON summary with:
   {
     "primary_hub": "<hub name>",
     "org": "<org>",
     "transport_mode": "air|sea|land",
     "eta_minutes": <int>,
     "resources": ["<resource1>", ...],
     "convoys": [<commit_hub_allocation results>]
   }

Prioritize hubs that specialize in the disaster type. For nearby events (<3000km) use air.
For distant events (>3000km) use sea. For same-continent events use land where feasible.""",
    tools=[list_available_hubs, calculate_aid_need, commit_hub_allocation],
)
