"""
Allocation tools — hub bidding, A2A logistics specialist call, and inventory queries.

The primary tool `run_allocation` either calls the local MasterAllocationAgent
or delegates to the remote A2A logistics specialist (port 8002) if available.
"""

from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.adk.tools.tool_context import ToolContext

log = logging.getLogger("crisisflow.adk.allocation")

# A2A logistics service endpoint (separate service, port 8002)
LOGISTICS_A2A_URL = os.getenv("LOGISTICS_A2A_URL", "http://localhost:8002")


def run_allocation(tool_context: ToolContext) -> str:
    """
    Orchestrate humanitarian aid allocation for the current crisis event.
    Solicits bids from all 6 UNHRD depots, uses AI to select the optimal combination,
    commits inventory, and returns a full convoy manifest.
    Writes allocation result to state['allocation_result'].
    First attempts to delegate to the remote A2A logistics specialist agent;
    falls back to local MasterAllocationAgent if the service is unavailable.
    """
    try:
        from crisisflow._tools.pipeline_tools import _event_from_state, _load_json
        from schema import ClassificationResult, SeverityResult
        event = _event_from_state(tool_context.state)
        cls_data = _load_json(tool_context.state, "classification_result", {})
        sev_data = _load_json(tool_context.state, "severity_result", {})

        ev_type = cls_data.get("event_type", event.type)
        severity_score = sev_data.get("score", event.severity)

        # Try A2A logistics specialist first
        a2a_result = _call_a2a_logistics(ev_type, severity_score, event)
        if a2a_result:
            tool_context.state["allocation_result"] = json.dumps(a2a_result)
            tool_context.state["allocation_source"] = "a2a_logistics_agent"
            return (
                f"[A2A] Allocation via logistics specialist: "
                f"primary={a2a_result.get('depot_name','?')} "
                f"eta={a2a_result.get('eta_minutes','?')}min "
                f"hubs={len(a2a_result.get('convoys', []))}"
            )

        # Fall back to local allocation
        return _run_local_allocation(tool_context, event, ev_type, severity_score)

    except Exception as exc:
        log.error("run_allocation failed: %s", exc)
        fallback = {
            "resources": ["assessment team"],
            "eta_minutes": 120,
            "depot_name": "Unknown",
            "depot_org": "",
            "convoys": [],
        }
        tool_context.state["allocation_result"] = json.dumps(fallback)
        return f"Allocation error (using fallback): {exc}"


def _call_a2a_logistics(ev_type: str, severity: int, event) -> dict | None:
    """
    Attempt to call the remote A2A logistics specialist agent.
    Returns allocation dict if successful, None if service is unavailable.
    """
    import requests
    try:
        payload = {
            "message": (
                f"Allocate humanitarian aid for a {ev_type} disaster. "
                f"Severity: {severity}/5. "
                f"Location: {event.location_name or f'({event.lat:.2f}, {event.lon:.2f})'}. "
                f"Affected population: {event.affected_population:,}. "
                f"Event lat: {event.lat}, lon: {event.lon}."
            )
        }
        r = requests.post(f"{LOGISTICS_A2A_URL}/run", json=payload, timeout=5)
        if r.status_code == 200:
            data = r.json()
            # Parse A2A response into our allocation schema
            return {
                "resources":   data.get("resources", []),
                "eta_minutes": data.get("eta_minutes", 0),
                "depot_name":  data.get("primary_hub", ""),
                "depot_org":   data.get("org", ""),
                "transport_mode": data.get("transport_mode", "air"),
                "convoys":     data.get("convoys", []),
            }
    except Exception as exc:
        log.debug("A2A logistics specialist unavailable: %s", exc)
    return None


def _run_local_allocation(tool_context: ToolContext, event, ev_type: str, severity_score: int) -> str:
    """Run local MasterAllocationAgent as fallback."""
    from hub_agents import MasterAllocationAgent
    from agents import RESOURCE_PLANS
    import feedback_store

    plans = RESOURCE_PLANS.get(ev_type, RESOURCE_PLANS.get("storm", {}))
    resources = plans.get(severity_score, plans.get(2, ["assessment team"]))
    feedback_ctx = feedback_store.get_prompt_context(ev_type)

    claude = None
    try:
        from claude_manager import ClaudeManager
        claude = ClaudeManager()
    except Exception:
        pass

    allocation = MasterAllocationAgent().run(
        event_type=ev_type,
        severity=severity_score,
        affected_population=event.affected_population,
        event_lat=event.lat,
        event_lon=event.lon,
        resource_list=resources,
        claude=claude,
        feedback_ctx=feedback_ctx,
    )

    payload = {
        "resources":      resources,
        "eta_minutes":    allocation.primary_eta_min,
        "depot_lat":      allocation.primary_hub_lat,
        "depot_lon":      allocation.primary_hub_lon,
        "depot_name":     allocation.primary_hub_name,
        "depot_org":      allocation.primary_hub_org,
        "transport_mode": allocation.transport_mode,
        "convoys":        allocation.to_dict()["convoys"],
        "need":           allocation.need.to_dict(),
        "total_committed": allocation.total_committed,
        "reasoning":      allocation.allocation_reasoning,
    }
    tool_context.state["allocation_result"] = json.dumps(payload)
    tool_context.state["allocation_source"] = "local_master_agent"
    return (
        f"[Local] Allocation: primary={allocation.primary_hub_name} "
        f"hubs={len(allocation.selected_bids)} "
        f"eta={allocation.primary_eta_min}min "
        f"transport={allocation.transport_mode}"
    )


def get_inventory_status(tool_context: ToolContext) -> str:
    """
    Return the current stock levels at all 6 UNHRD humanitarian depots.
    Useful for checking capacity before allocating resources.
    """
    try:
        import depot_inventory as inv
        status_lines = []
        for hub_name in [
            "Brindisi UNHRD", "Dubai UNHRD", "Accra UNHRD",
            "Kuala Lumpur UNHRD", "Panama City UNHRD", "Las Palmas UNHRD",
        ]:
            stock = inv.get_inventory(hub_name)
            if stock:
                summary = ", ".join(
                    f"{k}={v}" for k, v in stock.items()
                    if isinstance(v, (int, float)) and v > 0
                )
                status_lines.append(f"{hub_name}: {summary}")
        result = "\n".join(status_lines) if status_lines else "No inventory data available."
        tool_context.state["inventory_status"] = result
        return result
    except Exception as exc:
        return f"Inventory check failed: {exc}"
