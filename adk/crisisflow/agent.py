"""
CrisisFlow ADK Root Agent

Entry point for `adk web` and `adk run`. Defines `root_agent`.

Architecture:
  SequentialAgent
  ├── IngestAgent (ingest_agent)
  │     ├── ParallelAgent [USGS | NOAA | GDACS | EONET] ← parallel fetch
  │     └── prioritize_agent ← selects highest-severity event
  ├── PipelineAgent (pipeline_agent)
  │     ├── detection_agent
  │     ├── classification_agent
  │     ├── severity_agent
  │     ├── allocation_agent  ← tries A2A logistics, falls back to local
  │     ├── coordinator_agent
  │     ├── simulation_agent
  │     └── storage_agent
  └── ReflectionLoop (reflection_loop)
        └── reflection_agent × 2  ← LoopAgent, records feedback

To run (interactive CLI):
  cd crisis-flow
  adk run adk

To start the FastAPI + dev UI:
  cd crisis-flow
  adk api_server adk --port 8001

The A2A logistics specialist (optional, start first in a separate terminal):
  cd crisis-flow
  adk api_server adk/logistics_agent --port 8002
"""

from __future__ import annotations

import os
import sys

# crisisflow/agent.py lives at: crisis-flow/adk/crisisflow/agent.py
# ADK loads this as "crisisflow.agent" from the adk/ dir, so we need:
#   sys.path[0] = crisis-flow/         (for schema, agents, etc.)
#   sys.path[1] = crisis-flow/adk/     (for adk.agents.*, adk.tools.*)
_adk_dir      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # crisis-flow/adk/
_project_dir  = os.path.dirname(_adk_dir)                                     # crisis-flow/
sys.path.insert(0, _adk_dir)
sys.path.insert(0, _project_dir)

from dotenv import load_dotenv
load_dotenv(os.path.join(_project_dir, ".env"))

from google.adk.agents import LlmAgent, SequentialAgent

from crisisflow._agents.ingest_agent import ingest_agent
from crisisflow._agents.pipeline_agent import pipeline_agent
from crisisflow._agents.reflection_loop import reflection_loop

MODEL = "gemini-2.5-flash"

# ── Root agent ─────────────────────────────────────────────────────────────

root_agent = SequentialAgent(
    name="crisisflow_root",
    sub_agents=[
        ingest_agent,
        pipeline_agent,
        reflection_loop,
    ],
)
