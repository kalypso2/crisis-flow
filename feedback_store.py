"""
Feedback Store — in-memory ring buffer of Reflection Agent assessments.

After each event is processed, the Reflection Agent's critique is recorded here
indexed by event type. Before Severity and Allocation run on the next similar event,
they query this store and receive the past assessments as prompt context — creating
a real feedback loop where past mistakes inform future decisions.

Thread-safe. Survives only for the process lifetime (no persistence needed;
the loop builds up naturally as the pipeline processes events).
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque

log = logging.getLogger("crisisflow.feedback")

_lock = threading.Lock()

# Per event_type deque of recent reflection entries (maxlen=5 per type)
_store: dict[str, deque] = defaultdict(lambda: deque(maxlen=5))

# Running tally: how often each type was under/over/appropriate
_tally: dict[str, dict[str, int]] = defaultdict(lambda: {"under-resourced": 0, "over-resourced": 0, "appropriate": 0})


def record(
    event_type: str,
    assessment: str,
    concerns: str,
    suggestions: str,
    confidence: float,
    location: str = "",
) -> None:
    """
    Record a Reflection Agent assessment for a given event type.
    Called from main.py after reflection_result is available.
    """
    entry = {
        "assessment":   assessment,
        "concerns":     concerns.strip(),
        "suggestions":  suggestions.strip(),
        "confidence":   confidence,
        "location":     location.strip(),
    }
    with _lock:
        _store[event_type].append(entry)
        if assessment in _tally[event_type]:
            _tally[event_type][assessment] += 1
    log.debug("Feedback recorded: type=%s assessment=%s", event_type, assessment)


def get_prompt_context(event_type: str, n: int = 3) -> str:
    """
    Return a formatted string of the last n reflection assessments for this
    event type, ready to be injected into an agent prompt.

    Returns empty string if no feedback exists yet.
    """
    with _lock:
        recent = list(_store[event_type])[-n:]
        tally = dict(_tally[event_type])

    if not recent:
        return ""

    total = sum(tally.values())
    under = tally.get("under-resourced", 0)
    over  = tally.get("over-resourced", 0)
    ok    = tally.get("appropriate", 0)

    lines = [
        f"=== PAST RESPONSE FEEDBACK ({event_type}, last {len(recent)} events) ===",
        f"Historical pattern — appropriate: {ok}/{total} | under-resourced: {under}/{total} | over-resourced: {over}/{total}",
    ]

    for i, r in enumerate(recent, 1):
        loc = f" [{r['location']}]" if r["location"] else ""
        line = f"{i}.{loc} Assessment: {r['assessment']}"
        if r["concerns"]:
            line += f" | Concerns: {r['concerns'][:150]}"
        if r["suggestions"]:
            line += f" | Suggestions: {r['suggestions'][:150]}"
        lines.append(line)

    lines.append(
        "Use this feedback to calibrate your response. "
        "If past events were frequently under-resourced, score higher / allocate more. "
        "If over-resourced, be more conservative."
    )

    return "\n".join(lines)
