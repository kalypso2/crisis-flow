"""
Coordinator Agent — reviews all pipeline results and makes a final decision.
Debate Agent — resolves disagreements between severity estimates with LLM reasoning.

Both agents use Claude Opus via ClaudeManager.
All failures are non-fatal: the pipeline continues with the rule-based results.
"""

from __future__ import annotations

import json
import logging

from schema import (
    CrisisEvent,
    CoordinatorResult,
    DebateResult,
    ReflectionResult,
    SimulationResult,
)

log = logging.getLogger(__name__)


def _parse_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove opening fence (```json or ```) and closing fence
        raw = "\n".join(
            line for line in lines[1:]
            if not line.strip().startswith("```")
        )
    return json.loads(raw)


# ═══════════════════════════════════════════════════════════════════════════
# Coordinator Agent
# ═══════════════════════════════════════════════════════════════════════════

class CoordinatorAgent:
    """
    Reviews all pipeline agent outputs and produces a final coherent decision.

    Checks:
      - Internal consistency between classification, severity, and allocation
      - Whether severity should be overridden (only when clearly wrong)
      - Final recommendation: proceed | escalate | downgrade

    Emits: CoordinatorResult(final_recommendation, severity_override,
                             inconsistency_detected, reasoning, confidence)
    """

    def run(self, event: CrisisEvent, classification, severity, allocation, claude) -> CoordinatorResult:
        ev_type = classification.event_type if classification else event.type
        sev_score = severity.score if severity else event.severity
        sev_reasoning = getattr(severity, "reasoning", "") or getattr(severity, "reason", "")
        resources = ", ".join(allocation.resources) if allocation else "none"
        depot = allocation.depot_name if allocation else "unknown"
        eta = allocation.eta_minutes if allocation else 0

        prompt = f"""You are the Coordinator Agent for CrisisFlow, a global disaster response AI system.

Review these agent results for the crisis event below. Determine if they are consistent and make a final call.

=== EVENT ===
Title: {event.title}
Type: {ev_type}
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Source: {event.source}
Affected population: {event.affected_population:,}

=== AGENT RESULTS ===
Severity score: {sev_score}/5
Severity reasoning: {sev_reasoning}
Resources allocated: {resources}
Primary depot: {depot} — ETA {eta} min

=== YOUR TASK ===
1. Are the results internally consistent? (e.g. severity 5 earthquake should not get minimal resources)
2. Should severity be overridden? Only if clearly wrong — otherwise leave null.
3. What is your final recommendation?

Return ONLY valid JSON, no markdown:
{{
    "final_recommendation": "proceed" or "escalate" or "downgrade",
    "severity_override": null or <int 1-5>,
    "severity_override_reason": "<why, or empty string>",
    "inconsistency_detected": <bool>,
    "inconsistency_description": "<what, or empty string>",
    "confidence": <float 0.0-1.0>,
    "reasoning": "<2-3 sentence summary of the situation and your decision>"
}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            data = _parse_json(raw)

            override = data.get("severity_override")
            if override is not None:
                override = int(override)

            return CoordinatorResult(
                agent_name="coordinator",
                reason=f"recommendation={data.get('final_recommendation', 'proceed')}",
                confidence=float(data.get("confidence", 0.9)),
                reasoning=data.get("reasoning", ""),
                final_recommendation=data.get("final_recommendation", "proceed"),
                severity_override=override,
                inconsistency_detected=bool(data.get("inconsistency_detected", False)),
            )

        except Exception as exc:
            log.warning("CoordinatorAgent failed: %s", exc)
            return CoordinatorResult(
                agent_name="coordinator",
                reason="coordinator unavailable — proceeding with pipeline results",
                final_recommendation="proceed",
            )


# ═══════════════════════════════════════════════════════════════════════════
# Debate Agent
# ═══════════════════════════════════════════════════════════════════════════

class DebateAgent:
    """
    Called when the rule-based severity score and the adapter's initial severity
    estimate diverge by >= 2 points.

    Conducts a structured debate between the two positions and returns a
    winning score with justification.

    Emits: DebateResult(winning_position, winning_score, debate_summary, confidence)
    """

    def run(
        self,
        event: CrisisEvent,
        position_a_score: int,
        position_a_reason: str,
        position_b_score: int,
        position_b_reason: str,
        claude,
    ) -> DebateResult:

        prompt = f"""You are the Debate Arbiter for CrisisFlow, a global disaster response system.

Two analytical methods produced DIFFERENT severity scores for the same crisis event. Determine which is correct.

=== EVENT ===
Title: {event.title}
Source: {event.source}
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Affected population: {event.affected_population:,}

=== POSITION A — Initial Adapter Estimate ===
Score: {position_a_score}/5
Reasoning: {position_a_reason}

=== POSITION B — Rule-Based Agent Score ===
Score: {position_b_score}/5
Reasoning: {position_b_reason}

=== YOUR TASK ===
Evaluate both positions. Consider:
- Which is more grounded in the actual event data?
- Which better accounts for humanitarian impact?
- Is a synthesis of both more accurate?

Return ONLY valid JSON, no markdown:
{{
    "winning_score": <int 1-5>,
    "winning_position": "A" or "B" or "synthesis",
    "debate_summary": "<2-3 sentences explaining the decision>",
    "confidence": <float 0.0-1.0>
}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            data = _parse_json(raw)

            return DebateResult(
                agent_name="debate",
                reason=f"position={data.get('winning_position', 'A')} score={data.get('winning_score', position_a_score)}",
                confidence=float(data.get("confidence", 0.85)),
                reasoning=data.get("debate_summary", ""),
                winning_position=data.get("winning_position", "A"),
                winning_score=int(data.get("winning_score", position_a_score)),
                debate_summary=data.get("debate_summary", ""),
            )

        except Exception as exc:
            log.warning("DebateAgent failed: %s", exc)
            return DebateResult(
                agent_name="debate",
                reason="debate agent unavailable — keeping rule-based score",
                winning_score=position_b_score,
                winning_position="B",
            )


# ═══════════════════════════════════════════════════════════════════════════
# Reflection Agent
# ═══════════════════════════════════════════════════════════════════════════

class ReflectionAgent:
    """
    Post-decision self-assessment: reviews all agent outputs and critiques
    the response for appropriateness and completeness.

    Emits: ReflectionResult(assessment, concerns, suggestions, reasoning, confidence)
    """

    def run(
        self,
        event: CrisisEvent,
        severity,
        allocation,
        coordinator_result,
        claude,
    ) -> ReflectionResult:
        sev_score = severity.score if severity else event.severity
        resources = ", ".join(allocation.resources) if allocation else "none"
        depot = allocation.depot_name if allocation else "unknown"
        eta = allocation.eta_minutes if allocation else 0
        coord_rec = coordinator_result.final_recommendation if coordinator_result else "proceed"
        coord_reasoning = getattr(coordinator_result, "reasoning", "") if coordinator_result else ""

        prompt = f"""You are the Reflection Agent for CrisisFlow, a global disaster response AI system.
Review this crisis response decision for quality and completeness.

=== CRISIS EVENT ===
Title: {event.title}
Type: {event.type}
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Severity: {sev_score}/5
Affected population: {event.affected_population:,}
Source: {event.source}

=== RESPONSE DECISION ===
Resources allocated: {resources}
Primary depot: {depot} — ETA {eta} min
Coordinator recommendation: {coord_rec}
Coordinator reasoning: {coord_reasoning}

Critically assess this response:
1. Is the severity rating appropriate?
2. Are the resources proportionate (not under- or over-resourced)?
3. What concerns or gaps exist?
4. What would you recommend doing differently?

Return ONLY valid JSON, no markdown:
{{
    "assessment": "appropriate" or "under-resourced" or "over-resourced",
    "concerns": "<key concerns or empty string>",
    "suggestions": "<improvement suggestions or empty string>",
    "confidence": <float 0.0-1.0>,
    "reasoning": "<2-3 sentence summary>"
}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            data = _parse_json(raw)

            return ReflectionResult(
                agent_name="reflection",
                reason=f"assessment={data.get('assessment', 'appropriate')}",
                confidence=float(data.get("confidence", 0.85)),
                reasoning=data.get("reasoning", ""),
                assessment=str(data.get("assessment", "appropriate")),
                concerns=str(data.get("concerns", "")),
                suggestions=str(data.get("suggestions", "")),
            )

        except Exception as exc:
            log.warning("ReflectionAgent failed: %s", exc)
            return ReflectionResult(
                agent_name="reflection",
                reason="reflection agent unavailable",
            )


# ═══════════════════════════════════════════════════════════════════════════
# Simulation Agent
# ═══════════════════════════════════════════════════════════════════════════

class SimulationAgent:
    """
    "What if this worsens?" escalation scenario projector.

    Emits: SimulationResult(projected_severity, projected_affected_population,
                             additional_resources, pre_position_hubs, early_warning)
    """

    def run(self, event: CrisisEvent, severity, claude) -> SimulationResult:
        current_score = severity.score if severity else event.severity
        projected_score = min(5, current_score + 1)

        prompt = f"""You are the Simulation Agent for CrisisFlow, a global disaster response system.
Project what happens if this disaster escalates to the next severity level.

=== CURRENT EVENT ===
Title: {event.title}
Type: {event.type}
Location: {event.location_name or f"({event.lat:.2f}, {event.lon:.2f})"}
Current severity: {current_score}/5
Current affected population: {event.affected_population:,}
Source: {event.source}

=== ESCALATION SCENARIO ===
Project for severity {projected_score}/5:
1. Estimated affected population if escalation occurs
2. Additional resources needed beyond current allocation
3. Which humanitarian hubs should pre-position supplies now as a precaution
4. Actionable early warning message

Return ONLY valid JSON, no markdown:
{{
    "projected_severity": {projected_score},
    "projected_affected_population": <int>,
    "additional_resources_needed": ["<resource1>", ...],
    "pre_position_hubs": ["<hub_name1>", ...],
    "early_warning": "<actionable early warning message>",
    "confidence": <float 0.0-1.0>,
    "reasoning": "<2-3 sentence projection rationale>"
}}"""

        try:
            raw = claude.call(prompt, use_cache=True)
            data = _parse_json(raw)

            return SimulationResult(
                agent_name="simulation",
                reason=f"projected_severity={data.get('projected_severity', projected_score)}",
                confidence=float(data.get("confidence", 0.75)),
                reasoning=data.get("reasoning", ""),
                projected_severity=int(data.get("projected_severity", projected_score)),
                projected_affected_population=int(data.get("projected_affected_population", 0)),
                additional_resources=list(data.get("additional_resources_needed", [])),
                pre_position_hubs=list(data.get("pre_position_hubs", [])),
                early_warning=str(data.get("early_warning", "")),
            )

        except Exception as exc:
            log.warning("SimulationAgent failed: %s", exc)
            return SimulationResult(
                agent_name="simulation",
                reason="simulation agent unavailable",
            )
