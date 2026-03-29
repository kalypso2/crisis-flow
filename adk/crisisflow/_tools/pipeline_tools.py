"""
Pipeline tools — one function per agent stage.

These tools wrap the existing CrisisFlow agent classes.
Each tool reads state['current_event'] (and any upstream results),
runs the corresponding agent, writes its result to state, and returns a summary string.

Called by sequential LlmAgents in the pipeline SequentialAgent.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.adk.tools.tool_context import ToolContext
from schema import (
    CrisisEvent,
    ClassificationResult,
    SeverityResult,
    AllocationResult,
    CoordinatorResult,
)
from agents import DetectionAgent, ClassificationAgent, SeverityAgent
from coordinator import CoordinatorAgent, ReflectionAgent, SimulationAgent
import feedback_store

log = logging.getLogger("crisisflow.adk.pipeline")

# ── Shared helpers ─────────────────────────────────────────────────────────

def _get_claude():
    from claude_manager import ClaudeManager
    return ClaudeManager()


def _event_from_state(state: dict) -> CrisisEvent:
    """Deserialize CrisisEvent from state['current_event']."""
    raw = state.get("current_event", "{}")
    d = json.loads(raw) if isinstance(raw, str) else raw
    if not d:
        raise ValueError("No current_event in state")

    # Fix timestamp — CrisisEvent expects datetime, JSON has ISO string
    ts_raw = d.get("timestamp")
    if isinstance(ts_raw, str):
        try:
            d = dict(d)
            d["timestamp"] = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except Exception:
            d["timestamp"] = datetime.now(timezone.utc)

    # Only pass fields that CrisisEvent knows about
    known = set(CrisisEvent.__dataclass_fields__.keys())
    filtered = {k: v for k, v in d.items() if k in known}
    return CrisisEvent(**filtered)


def _load_json(state: dict, key: str, fallback: dict) -> dict:
    raw = state.get(key, "{}")
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or fallback)
    except Exception:
        return fallback


# ── Tool functions ─────────────────────────────────────────────────────────

def run_detection(tool_context: ToolContext) -> str:
    """
    Run the Detection Agent on the current crisis event.
    Decides if the event is actionable and worth escalating.
    Writes detection result to state['detection_result'].
    """
    try:
        event = _event_from_state(tool_context.state)
        result = DetectionAgent().run(event, claude=_get_claude())
        payload = {
            "accepted":   result.accepted,
            "reason":     result.reason,
            "confidence": result.confidence,
            "reasoning":  result.reasoning,
        }
        tool_context.state["detection_result"] = json.dumps(payload)

        if not result.accepted:
            tool_context.state["pipeline_halted"] = "true"
            return f"REJECTED: {result.reason} (confidence={result.confidence:.2f})"

        return f"ACCEPTED: {result.reason} (confidence={result.confidence:.2f})"

    except Exception as exc:
        log.error("run_detection failed: %s", exc)
        tool_context.state["detection_result"] = json.dumps({"accepted": True, "reason": str(exc)})
        return f"Detection error (defaulting to accepted): {exc}"


def run_classification(tool_context: ToolContext) -> str:
    """
    Run the Classification Agent on the current crisis event.
    Assigns a canonical event type and domain tags.
    Writes classification result to state['classification_result'].
    """
    try:
        event = _event_from_state(tool_context.state)
        result = ClassificationAgent().run(event, claude=_get_claude())
        payload = {
            "event_type":  result.event_type,
            "domain_tags": result.domain_tags,
            "confidence":  result.confidence,
            "reasoning":   result.reasoning,
            "reason":      result.reason,
        }
        tool_context.state["classification_result"] = json.dumps(payload)
        return (
            f"Classified as {result.event_type.upper()} "
            f"tags={result.domain_tags} confidence={result.confidence:.2f}"
        )
    except Exception as exc:
        log.error("run_classification failed: %s", exc)
        tool_context.state["classification_result"] = json.dumps({
            "event_type": "unknown", "domain_tags": [], "confidence": 0.5, "reason": str(exc)
        })
        return f"Classification error: {exc}"


def run_severity(tool_context: ToolContext) -> str:
    """
    Run the Severity Agent on the current crisis event.
    Produces a 1-5 severity score using source signals and population impact.
    Writes severity result to state['severity_result'].
    """
    try:
        event = _event_from_state(tool_context.state)
        cls_data = _load_json(tool_context.state, "classification_result", {})
        classification = ClassificationResult(
            agent_name="classification",
            event_type=cls_data.get("event_type", event.type),
            domain_tags=cls_data.get("domain_tags", []),
            reason=cls_data.get("reason", ""),
            reasoning=cls_data.get("reasoning", ""),
            confidence=cls_data.get("confidence", 0.8),
        )
        feedback_ctx = feedback_store.get_prompt_context(event.type)
        result = SeverityAgent().run(event, classification, claude=_get_claude(), feedback_ctx=feedback_ctx)
        payload = {
            "score":      result.score,
            "raw_score":  result.raw_score,
            "reason":     result.reason,
            "reasoning":  result.reasoning,
            "confidence": result.confidence,
        }
        tool_context.state["severity_result"] = json.dumps(payload)
        return f"Severity score: {result.score}/5 (confidence={result.confidence:.2f})"

    except Exception as exc:
        log.error("run_severity failed: %s", exc)
        tool_context.state["severity_result"] = json.dumps({"score": 3, "raw_score": 3.0, "reason": str(exc)})
        return f"Severity error (defaulting to 3): {exc}"


def run_coordinator(tool_context: ToolContext) -> str:
    """
    Run the Coordinator Agent to review all upstream results and make a final decision.
    Checks for inconsistencies and may override severity if clearly wrong.
    Writes coordinator result to state['coordinator_result'].
    """
    try:
        event = _event_from_state(tool_context.state)
        cls_data  = _load_json(tool_context.state, "classification_result", {})
        sev_data  = _load_json(tool_context.state, "severity_result", {})
        alloc_data = _load_json(tool_context.state, "allocation_result", {})

        classification = ClassificationResult(
            agent_name="classification",
            event_type=cls_data.get("event_type", "unknown"),
            domain_tags=cls_data.get("domain_tags", []),
            reason="", reasoning=cls_data.get("reasoning", ""),
            confidence=cls_data.get("confidence", 0.8),
        )
        severity = SeverityResult(
            agent_name="severity",
            score=sev_data.get("score", 3),
            raw_score=float(sev_data.get("raw_score", 3.0)),
            reason=sev_data.get("reason", ""),
            reasoning=sev_data.get("reasoning", ""),
            confidence=sev_data.get("confidence", 0.8),
        )
        allocation = AllocationResult(
            agent_name="allocation",
            resources=alloc_data.get("resources", []),
            eta_minutes=alloc_data.get("eta_minutes", 0),
            depot_name=alloc_data.get("depot_name", ""),
            reason="",
        )

        result = CoordinatorAgent().run(event, classification, severity, allocation, claude=_get_claude())
        payload = {
            "final_recommendation":  result.final_recommendation,
            "severity_override":     result.severity_override,
            "inconsistency_detected": result.inconsistency_detected,
            "reasoning":             result.reasoning,
            "confidence":            result.confidence,
        }
        tool_context.state["coordinator_result"] = json.dumps(payload)
        override_str = f" | override→{result.severity_override}" if result.severity_override else ""
        return f"Decision: {result.final_recommendation.upper()}{override_str} (confidence={result.confidence:.2f})"

    except Exception as exc:
        log.error("run_coordinator failed: %s", exc)
        tool_context.state["coordinator_result"] = json.dumps({
            "final_recommendation": "proceed", "severity_override": None,
            "inconsistency_detected": False, "reasoning": str(exc), "confidence": 0.5,
        })
        return f"Coordinator error (defaulting to proceed): {exc}"


def run_reflection(tool_context: ToolContext) -> str:
    """
    Run the Reflection Agent to critically assess the response quality.
    Records feedback into the feedback store to improve future decisions.
    Writes reflection result to state['reflection_result'].
    """
    try:
        event     = _event_from_state(tool_context.state)
        sev_data  = _load_json(tool_context.state, "severity_result", {})
        alloc_data = _load_json(tool_context.state, "allocation_result", {})
        coord_data = _load_json(tool_context.state, "coordinator_result", {})

        severity = SeverityResult(
            agent_name="severity",
            score=sev_data.get("score", 3),
            raw_score=float(sev_data.get("raw_score", 3.0)),
            reason="", reasoning=sev_data.get("reasoning", ""),
            confidence=sev_data.get("confidence", 0.8),
        )
        allocation = AllocationResult(
            agent_name="allocation",
            resources=alloc_data.get("resources", []),
            eta_minutes=alloc_data.get("eta_minutes", 0),
            depot_name=alloc_data.get("depot_name", ""),
            reason="",
        )
        coordinator = CoordinatorResult(
            agent_name="coordinator",
            reason="",
            final_recommendation=coord_data.get("final_recommendation", "proceed"),
            reasoning=coord_data.get("reasoning", ""),
        )

        result = ReflectionAgent().run(event, severity, allocation, coordinator, claude=_get_claude())

        # Write to feedback store so next similar event benefits
        feedback_store.record(
            event_type=event.type,
            assessment=result.assessment,
            concerns=result.concerns,
            suggestions=result.suggestions,
            confidence=result.confidence,
            location=event.location_name,
        )

        payload = {
            "assessment":  result.assessment,
            "concerns":    result.concerns,
            "suggestions": result.suggestions,
            "reasoning":   result.reasoning,
            "confidence":  result.confidence,
        }
        tool_context.state["reflection_result"] = json.dumps(payload)
        return (
            f"Assessment: {result.assessment.upper()} | "
            f"Concerns: {(result.concerns or 'none')[:100]}"
        )

    except Exception as exc:
        log.error("run_reflection failed: %s", exc)
        tool_context.state["reflection_result"] = json.dumps({
            "assessment": "appropriate", "concerns": str(exc), "suggestions": "", "confidence": 0.5
        })
        return f"Reflection error: {exc}"


def run_simulation(tool_context: ToolContext) -> str:
    """
    Run the Simulation Agent to project what happens if the disaster escalates.
    Estimates projected population, additional resources, and pre-positioning hubs.
    Writes simulation result to state['simulation_result'].
    """
    try:
        event    = _event_from_state(tool_context.state)
        sev_data = _load_json(tool_context.state, "severity_result", {})
        severity = SeverityResult(
            agent_name="severity",
            score=sev_data.get("score", 3),
            raw_score=float(sev_data.get("raw_score", 3.0)),
            reason="",
        )

        result = SimulationAgent().run(event, severity, claude=_get_claude())
        payload = {
            "projected_severity":            result.projected_severity,
            "projected_affected_population": result.projected_affected_population,
            "additional_resources":          result.additional_resources,
            "pre_position_hubs":             result.pre_position_hubs,
            "early_warning":                 result.early_warning,
        }
        tool_context.state["simulation_result"] = json.dumps(payload)
        return (
            f"If escalates to Sev {result.projected_severity}: "
            f"~{result.projected_affected_population:,} affected. "
            f"Pre-position: {result.pre_position_hubs}"
        )

    except Exception as exc:
        log.error("run_simulation failed: %s", exc)
        tool_context.state["simulation_result"] = json.dumps({"error": str(exc)})
        return f"Simulation error: {exc}"


def store_event_result(tool_context: ToolContext) -> str:
    """
    Persist the fully-processed event (with all agent reasoning) to Snowflake
    and broadcast it to SSE listeners. Call this as the final pipeline step.
    """
    try:
        import snowflake_store

        event  = _event_from_state(tool_context.state)
        det    = _load_json(tool_context.state, "detection_result", {})
        cls    = _load_json(tool_context.state, "classification_result", {})
        sev    = _load_json(tool_context.state, "severity_result", {})
        alloc  = _load_json(tool_context.state, "allocation_result", {})
        coord  = _load_json(tool_context.state, "coordinator_result", {})
        refl   = _load_json(tool_context.state, "reflection_result", {})
        sim    = _load_json(tool_context.state, "simulation_result", {})

        # Apply coordinator severity override if present
        override = coord.get("severity_override")
        if override:
            event.severity = int(override)

        # Update event fields from pipeline results
        event.type        = cls.get("event_type", event.type)
        event.domain_tags = cls.get("domain_tags", event.domain_tags)
        event.severity    = sev.get("score", event.severity)
        event.allocation  = alloc
        event.agent_reasoning = {
            "detection":      det,
            "classification": cls,
            "severity":       sev,
            "allocation":     alloc,
            "coordinator":    coord,
            "reflection":     refl,
            "simulation":     sim,
        }

        snowflake_store.store_event(event.to_dict())
        return f"Stored event '{event.title}' (severity={event.severity}) to Snowflake."

    except Exception as exc:
        log.error("store_event_result failed: %s", exc)
        return f"Storage error: {exc}"
