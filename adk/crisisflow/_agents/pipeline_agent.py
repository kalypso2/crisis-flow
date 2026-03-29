"""
Pipeline Agent — SequentialAgent running all 7 analysis stages in order.

Stages:
  1. detection_agent       — gatekeeper: is this actionable?
  2. classification_agent  — type + domain tags
  3. severity_agent        — 1-5 score with population/source signals
  4. allocation_agent      — multi-hub convoy manifest (A2A or local)
  5. coordinator_agent     — final holistic review + optional severity override
  6. simulation_agent      — escalation projection
  7. storage_agent         — persist to Snowflake + SSE broadcast

Each stage reads from session state and writes its result back to state.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.adk.agents import LlmAgent, SequentialAgent

from crisisflow._tools.pipeline_tools import (
    run_detection,
    run_classification,
    run_severity,
    run_coordinator,
    run_simulation,
    store_event_result,
)
from crisisflow._tools.allocation_tools import run_allocation

MODEL = "gemini-2.5-flash"

# ── Stage 1: Detection ─────────────────────────────────────────────────────

_detection_agent = LlmAgent(
    name="detection_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Detection Agent — the pipeline gatekeeper. "
        "Call the run_detection tool on the current event from state. "
        "Determine if the event is actionable and worth escalating resources. "
        "Report whether the event was accepted or rejected and why."
    ),
    tools=[run_detection],
    output_key="detection_summary",
)

# ── Stage 2: Classification ────────────────────────────────────────────────

_classification_agent = LlmAgent(
    name="classification_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Classification Agent. "
        "Call the run_classification tool to assign the canonical event type "
        "(earthquake/flood/cyclone/volcano/wildfire/drought/storm/iceberg) "
        "and domain tags to the current event. "
        "Report the assigned type and tags."
    ),
    tools=[run_classification],
    output_key="classification_summary",
)

# ── Stage 3: Severity Scoring ──────────────────────────────────────────────

_severity_agent = LlmAgent(
    name="severity_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Severity Agent. "
        "Call the run_severity tool to produce a 1-5 severity score for the current event. "
        "The tool applies source-specific rules (USGS magnitude, NOAA severity strings, "
        "GDACS alert levels) and incorporates feedback from past similar events. "
        "Report the severity score and the key factors driving it."
    ),
    tools=[run_severity],
    output_key="severity_summary",
)

# ── Stage 4: Resource Allocation ──────────────────────────────────────────

_allocation_agent = LlmAgent(
    name="allocation_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Allocation Agent. "
        "Call the run_allocation tool to orchestrate humanitarian aid from the 6 UNHRD depots. "
        "The tool will contact the A2A logistics specialist agent if available, "
        "or run the local MasterAllocationAgent as fallback. "
        "Report which hubs are deploying, estimated ETAs, and transport modes."
    ),
    tools=[run_allocation],
    output_key="allocation_summary",
)

# ── Stage 5: Coordinator Review ────────────────────────────────────────────

_coordinator_agent = LlmAgent(
    name="coordinator_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Coordinator Agent — the final decision reviewer. "
        "Call the run_coordinator tool to check all upstream agent results for consistency. "
        "The coordinator can override severity if clearly wrong and makes the final "
        "proceed/escalate/downgrade recommendation. "
        "Report the final decision and any inconsistencies found."
    ),
    tools=[run_coordinator],
    output_key="coordinator_summary",
)

# ── Stage 6: Escalation Simulation ────────────────────────────────────────

_simulation_agent = LlmAgent(
    name="simulation_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Simulation Agent. "
        "Call the run_simulation tool to project what happens if this disaster "
        "escalates to the next severity level. "
        "Report the projected population impact, additional resources needed, "
        "and which hubs should pre-position supplies now."
    ),
    tools=[run_simulation],
    output_key="simulation_summary",
)

# ── Stage 7: Persist Results ───────────────────────────────────────────────

_storage_agent = LlmAgent(
    name="storage_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Storage Agent. "
        "Call the store_event_result tool to save the fully-processed crisis event "
        "with all agent reasoning to Snowflake and broadcast it via SSE. "
        "Report confirmation that the event was stored."
    ),
    tools=[store_event_result],
    output_key="storage_summary",
)

# ── Public: pipeline_agent ─────────────────────────────────────────────────

pipeline_agent = SequentialAgent(
    name="pipeline_agent",
    sub_agents=[
        _detection_agent,
        _classification_agent,
        _severity_agent,
        _allocation_agent,
        _coordinator_agent,
        _simulation_agent,
        _storage_agent,
    ],
)
