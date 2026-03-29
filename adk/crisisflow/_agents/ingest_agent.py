"""
Ingest Agent — ParallelAgent that fires all 4 data sources simultaneously.

The 4 source agents run in parallel (USGS, NOAA, GDACS, EONET), each writing
their events to state. A prioritize agent then merges and selects the
highest-severity event for the pipeline to process.

This demonstrates ADK's ParallelAgent capability — all 4 sources fetch concurrently
instead of sequentially, reducing total ingest latency by ~3x.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

from crisisflow._tools.ingest_tools import (
    fetch_usgs_earthquakes,
    fetch_noaa_alerts,
    fetch_gdacs_disasters,
    fetch_eonet_events,
    prioritize_events,
)

MODEL = "gemini-2.5-flash"

# ── 4 parallel source agents ───────────────────────────────────────────────

_usgs_agent = LlmAgent(
    name="usgs_ingest_agent",
    model=MODEL,
    instruction=(
        "You are the USGS earthquake ingest agent for CrisisFlow. "
        "Call the fetch_usgs_earthquakes tool to download recent earthquake data. "
        "Report the number of events fetched."
    ),
    tools=[fetch_usgs_earthquakes],
    output_key="usgs_summary",
)

_noaa_agent = LlmAgent(
    name="noaa_ingest_agent",
    model=MODEL,
    instruction=(
        "You are the NOAA weather alert ingest agent for CrisisFlow. "
        "Call the fetch_noaa_alerts tool to download active US weather alerts. "
        "Report the number of alerts fetched."
    ),
    tools=[fetch_noaa_alerts],
    output_key="noaa_summary",
)

_gdacs_agent = LlmAgent(
    name="gdacs_ingest_agent",
    model=MODEL,
    instruction=(
        "You are the GDACS global disaster ingest agent for CrisisFlow. "
        "Call the fetch_gdacs_disasters tool to download global disaster alerts. "
        "Report the number of events fetched."
    ),
    tools=[fetch_gdacs_disasters],
    output_key="gdacs_summary",
)

_eonet_agent = LlmAgent(
    name="eonet_ingest_agent",
    model=MODEL,
    instruction=(
        "You are the NASA EONET natural event ingest agent for CrisisFlow. "
        "Call the fetch_eonet_events tool to download active natural events. "
        "Report the number of events fetched."
    ),
    tools=[fetch_eonet_events],
    output_key="eonet_summary",
)

# ── Parallel fetch step ────────────────────────────────────────────────────

_parallel_fetch = ParallelAgent(
    name="parallel_source_fetch",
    sub_agents=[_usgs_agent, _noaa_agent, _gdacs_agent, _eonet_agent],
)

# ── Prioritize step ────────────────────────────────────────────────────────

_prioritize_agent = LlmAgent(
    name="prioritize_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow event prioritizer. "
        "Call the prioritize_events tool to merge all 4 source results from state, "
        "deduplicate events, and select the single highest-severity event for processing. "
        "Report which event was selected and why it was prioritized."
    ),
    tools=[prioritize_events],
    output_key="prioritize_summary",
)

# ── Public: ingest_agent ───────────────────────────────────────────────────

ingest_agent = SequentialAgent(
    name="ingest_agent",
    sub_agents=[_parallel_fetch, _prioritize_agent],
)
