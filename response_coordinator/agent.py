"""
Response Coordinator — A2A specialist agent.

Receives crisis intelligence briefings from the Intelligence Analyst
and generates actionable humanitarian response plans with structured
alert payloads. Deployed as a separate A2A-discoverable agent.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from .tools import generate_alert_payload, update_response_database

MODEL = "gemini-2.5-flash-lite"

root_agent = LlmAgent(
    name="HumanitarianResponseCoordinator",
    model=MODEL,
    instruction=(
        "You are the Humanitarian Response Coordinator for CrisisFlow — "
        "a specialist agent that receives crisis intelligence briefings and "
        "generates actionable response plans.\n\n"
        "When you receive a briefing, generate a comprehensive response plan:\n\n"
        "1. PRIORITY ACTIONS: Ranked list of immediate response needs with "
        "specific timelines (hours/days)\n"
        "2. RESOURCE ALLOCATION: Which CrisisFlow depots (Geneva, Nairobi, "
        "Bangkok, Mexico City, Sydney, Dubai) should respond to which zones, "
        "with estimated ETAs\n"
        "3. ALERT PAYLOADS: Call generate_alert_payload with a JSON array of "
        "structured alerts for each critical zone:\n"
        '   [{{"zone_id":"...","alert_level":"red","action_required":"...",'
        '"eta_hours":8,"resources":["medical units","evacuation teams"]}},...]'
        "\n"
        "4. COORDINATION NOTES: Cross-zone dependencies, compound risks, "
        "humanitarian corridor requirements\n\n"
        "After generating the plan, call update_response_database with the "
        "full response plan as a JSON object.\n\n"
        "Be specific: name the depots, estimate ETAs in hours, list exact "
        "resource types. This plan drives real-world response actions."
    ),
    tools=[generate_alert_payload, update_response_database],
    output_key="response_plan",
)
