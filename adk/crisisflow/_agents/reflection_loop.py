"""
Reflection Loop — LoopAgent with genuine self-correction.

Each iteration:
  1. run_reflection   → critiques the response: appropriate / under-resourced / over-resourced
  2. self_correct     → if under/over-resourced: adjusts severity, re-runs allocation, continues
                        if appropriate: sets escalate=True → loop exits early

This is real autonomous self-healing:
  - The agent changes its own decisions based on its own critique
  - The loop exits as soon as the response is calibrated
  - Max 3 iterations prevents runaway loops

ADK Dev UI trace shows exactly:
  Iter 1: reflection=under-resourced → self_correct: sev 3→4, re-allocate
  Iter 2: reflection=appropriate     → self_correct: escalate → loop done
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.adk.agents import LlmAgent, LoopAgent

from crisisflow._tools.pipeline_tools import run_reflection
from crisisflow._tools.correction_tools import self_correct

MODEL = "gemini-2.5-flash"

_reflection_agent = LlmAgent(
    name="reflection_agent",
    model=MODEL,
    instruction=(
        "You are the CrisisFlow Self-Correction Agent. Your job is to iteratively "
        "critique and fix the crisis response until it is appropriate.\n\n"
        "Each iteration, do this in order:\n"
        "1. Call run_reflection to critically assess whether the response is appropriate, "
        "under-resourced, or over-resourced. Be honest and specific.\n"
        "2. Call self_correct — it will read your assessment and either:\n"
        "   - Adjust severity and re-allocate resources (if under/over-resourced), OR\n"
        "   - Signal the loop to end (if appropriate or max corrections reached).\n\n"
        "After both tool calls, report:\n"
        "- The assessment result\n"
        "- What correction was applied (if any)\n"
        "- Whether the loop is continuing or exiting\n\n"
        "Do NOT skip self_correct. It must be called every iteration."
    ),
    tools=[run_reflection, self_correct],
    output_key="reflection_summary",
)

# ── Public: reflection_loop ────────────────────────────────────────────────

reflection_loop = LoopAgent(
    name="reflection_loop",
    sub_agents=[_reflection_agent],
    max_iterations=5,
)
