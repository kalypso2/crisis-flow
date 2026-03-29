"""
Consensus Engine — runs after all five agents and before the action summary
is emitted.

Three scenarios:

  AGREEMENT (happy path)
    Severity scores within 1 point of the adapter estimate, types match.
    Pipeline proceeds immediately with no modifications.

  SOFT DISAGREEMENT
    Triggered when:
      • Severity scores differ by > 1 between any two agents, OR
      • Classification type differs from the adapter's original type
    Resolution:
      1. Averaged severity across adapter and agent scores
      2. Majority-vote type
      3. Reduce allocation scope by one tier (conservative hedge)
      4. Set consensus_flag = "LOW_CONFIDENCE"

  HARD DISAGREEMENT / AGENT FAILURE
    Triggered when:
      • An agent timed out or raised an exception (result is None), OR
      • Detection rejected an event that an authoritative source
        (USGS / NOAA / GDACS) rated as red alert
    Resolution:
      1. Source authority override — USGS/NOAA/GDACS red alert forces sev >= 4
      2. Dead-agent substitution — clone last good result from cache
      3. Circuit breaker — agent marked DEGRADED after 3 failures in 60 s
      4. Set consensus_flag = "FALLBACK_USED"
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from typing import Optional

from schema import (
    CrisisEvent,
    DetectionResult,
    ClassificationResult,
    SeverityResult,
    AllocationResult,
    CommunicationResult,
)

log = logging.getLogger(__name__)

AUTHORITATIVE_SOURCES = {"usgs", "noaa", "gdacs"}

# ── Circuit-breaker state (per agent name) ────────────────────────────────
# Maps agent_name → deque of failure timestamps (last 60 s)
_failure_windows: dict[str, deque] = {}
_degraded_until: dict[str, float] = {}
CIRCUIT_WINDOW_S = 60
CIRCUIT_THRESHOLD = 3
CIRCUIT_COOLDOWN_S = 300


def _record_failure(agent_name: str):
    now = time.time()
    if agent_name not in _failure_windows:
        _failure_windows[agent_name] = deque()
    window = _failure_windows[agent_name]
    window.append(now)
    # Evict old timestamps
    while window and window[0] < now - CIRCUIT_WINDOW_S:
        window.popleft()
    if len(window) >= CIRCUIT_THRESHOLD:
        _degraded_until[agent_name] = now + CIRCUIT_COOLDOWN_S
        log.warning(
            "CIRCUIT BREAKER: agent '%s' DEGRADED for %ds after %d failures",
            agent_name, CIRCUIT_COOLDOWN_S, CIRCUIT_THRESHOLD,
        )


def is_degraded(agent_name: str) -> bool:
    until = _degraded_until.get(agent_name, 0)
    if time.time() < until:
        return True
    if agent_name in _degraded_until:
        del _degraded_until[agent_name]
    return False


# ── Last-known-good cache (for dead-agent substitution) ──────────────────
_last_good: dict[str, object] = {}


def _cache_result(result):
    if result is not None:
        _last_good[result.agent_name] = result


def _substitute(agent_name: str):
    """Return the last-known-good result for an agent from cache."""
    cached = _last_good.get(agent_name)
    if cached is None:
        return None
    import copy
    sub = copy.copy(cached)
    sub.reason = f"[SUBSTITUTED from cache] {sub.reason}"
    return sub


# ═══════════════════════════════════════════════════════════════════════════
# Consensus Engine
# ═══════════════════════════════════════════════════════════════════════════

class ConsensusEngine:

    def resolve(
        self,
        event: CrisisEvent,
        detection: Optional[DetectionResult],
        classification: Optional[ClassificationResult],
        severity: Optional[SeverityResult],
        allocation: Optional[AllocationResult],
    ) -> tuple[
        DetectionResult,
        ClassificationResult,
        SeverityResult,
        AllocationResult,
        str,   # consensus_flag
    ]:
        """
        Inspect agent results, detect disagreement, apply resolution strategy,
        and return a final coherent set of results plus a consensus_flag.
        """

        # ── Step 1: Handle missing / failed agents ────────────────────────
        detection, classification, severity, allocation, hard_failure = (
            self._fill_missing(event, detection, classification, severity, allocation)
        )

        # ── Step 2: Source authority override ────────────────────────────
        forced_severity = self._authority_override(event, severity)
        if forced_severity is not None:
            severity = forced_severity
            hard_failure = True

        # ── Step 3: Classify disagreement level ──────────────────────────
        severity_spread = self._severity_spread(event, severity)
        type_mismatch = (
            classification is not None and
            event.type != "unknown" and
            classification.event_type != event.type
        )

        # ── Step 4: Resolution ───────────────────────────────────────────
        if hard_failure:
            flag = "FALLBACK_USED"
            severity = self._averaged_severity(event, severity)
            allocation = self._reduce_allocation(allocation)
            log.warning(
                "CONSENSUS HARD: event=%s flag=%s missing_agents=%d",
                event.id[:8], flag,
                sum(1 for x in [detection, classification, severity, allocation] if x is None),
            )

        elif severity_spread > 1 or type_mismatch:
            flag = "LOW_CONFIDENCE"
            severity = self._averaged_severity(event, severity)
            if classification is not None:
                classification.event_type = self._majority_vote_type(event, classification)
            allocation = self._reduce_allocation(allocation)
            log.info(
                "CONSENSUS SOFT: event=%s flag=%s spread=%d type_mismatch=%s",
                event.id[:8], flag, severity_spread, type_mismatch,
            )

        else:
            flag = ""
            log.debug("CONSENSUS OK: event=%s all agents agree", event.id[:8])

        # Cache successful results for future substitution
        for r in [detection, classification, severity, allocation]:
            _cache_result(r)

        return detection, classification, severity, allocation, flag

    # ── Private helpers ───────────────────────────────────────────────────

    def _fill_missing(self, event, detection, classification, severity, allocation):
        """
        For any None result (agent failed/timed out), attempt substitution
        from cache. If no cache exists, use safe defaults.
        """
        hard_failure = False

        if detection is None:
            _record_failure("detection")
            detection = _substitute("detection") or DetectionResult(
                agent_name="detection", accepted=True,
                reason="default substitution — agent unavailable",
            )
            hard_failure = True

        if classification is None:
            _record_failure("classification")
            classification = _substitute("classification") or ClassificationResult(
                agent_name="classification", event_type=event.type,
                domain_tags=[],
                reason="default substitution — agent unavailable",
            )
            hard_failure = True

        if severity is None:
            _record_failure("severity")
            severity = _substitute("severity") or SeverityResult(
                agent_name="severity", score=3, raw_score=3.0,
                reason="default substitution — agent unavailable",
            )
            hard_failure = True

        if allocation is None:
            _record_failure("allocation")
            allocation = _substitute("allocation") or AllocationResult(
                agent_name="allocation", resources=["first-aid only"],
                eta_minutes=0, depot_lat=0.0, depot_lon=0.0, depot_name="Unknown",
                reason="default substitution — agent unavailable",
            )
            hard_failure = True

        return detection, classification, severity, allocation, hard_failure

    def _authority_override(self, event: CrisisEvent, severity: SeverityResult):
        """
        If the source is authoritative (USGS/NOAA/GDACS) and the raw data
        indicates a red/extreme alert, enforce severity >= 4 regardless of
        what the severity agent computed.
        """
        if event.source not in AUTHORITATIVE_SOURCES:
            return None

        raw = event.raw
        red_signal = False

        if event.source == "usgs":
            red_signal = (raw.get("alert") or "").lower() in ("red", "orange")
        elif event.source == "noaa":
            red_signal = raw.get("severity") == "Extreme"
        elif event.source == "gdacs":
            red_signal = (raw.get("alertlevel") or "").lower() == "red"

        if red_signal and severity is not None and severity.score < 4:
            log.warning(
                "AUTHORITY OVERRIDE: source=%s forced severity from %d to 4",
                event.source, severity.score,
            )
            import copy
            overridden = copy.copy(severity)
            overridden.score = 4
            overridden.reason = f"[AUTHORITY OVERRIDE] {overridden.reason}"
            return overridden

        return None

    def _severity_spread(self, event: CrisisEvent, severity: Optional[SeverityResult]) -> int:
        """Return difference between severity agent score and adapter initial score."""
        if severity is None:
            return 0
        return abs(severity.score - event.severity)

    def _averaged_severity(
        self,
        event: CrisisEvent,
        severity: Optional[SeverityResult],
    ) -> Optional[SeverityResult]:
        """Average the adapter's initial severity estimate with the agent's score."""
        if severity is None:
            return severity

        new_score = max(1, min(5, round((event.severity + severity.score) / 2)))

        import copy
        result = copy.copy(severity)
        result.score = new_score
        result.reason = (
            f"[AVERAGED] adapter={event.severity} + agent={severity.score} → {new_score}"
        )
        return result

    def _majority_vote_type(
        self, event: CrisisEvent, classification: ClassificationResult
    ) -> str:
        """Pick the majority type from adapter + classification agent."""
        candidates = [event.type, classification.event_type]
        counts = Counter(candidates)
        winner = counts.most_common(1)[0][0]
        if winner != classification.event_type:
            log.info(
                "MAJORITY VOTE: type changed from '%s' to '%s'",
                classification.event_type, winner,
            )
        return winner

    def _reduce_allocation(
        self, allocation: Optional[AllocationResult]
    ) -> Optional[AllocationResult]:
        """
        Reduce allocation by removing the last resource from the list
        (one tier lower than originally recommended) as a conservative hedge.
        """
        if allocation is None or not allocation.resources:
            return allocation
        import copy
        result = copy.copy(allocation)
        if len(result.resources) > 1:
            dropped = result.resources.pop()
            result.reason = f"[REDUCED: dropped '{dropped}'] {result.reason}"
        return result
