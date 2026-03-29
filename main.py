"""
CrisisFlow main pipeline — orchestrates ingestion, agent execution,
consensus resolution, and SSE streaming to the dashboard.

Start with:
    python pipeline/main.py

Exposes:
    GET  /events        — last 100 processed events (JSON)
    GET  /stream        — Server-Sent Events stream of new events
    GET  /health        — agent health / circuit-breaker status
    GET  /pipeline/status
    POST /pipeline/poll-sources   — manual one-shot ingest (when auto pipeline off)
    POST /pipeline/process-next   — run agent pipeline on one queued event

Background ingestion + consumer are OFF by default. Set CRISISFLOW_AUTO_PIPELINE=1 to restore
continuous polling (stock will deplete as events are processed).
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()  # loads .env from project root

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from schema import CrisisEvent
from need_calculator import scrub_event_allocation_need_notes
from adapters import (
    USGSAdapter, NOAAAdapter, GDACSAdapter, EONETAdapter,
)
from agents import (
    DetectionAgent, ClassificationAgent, SeverityAgent,
    AllocationAgent, CommunicationAgent,
)
from consensus import ConsensusEngine, is_degraded
from coordinator import CoordinatorAgent, DebateAgent, ReflectionAgent, SimulationAgent
import snowflake_store
import feedback_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("pipeline.main")


def _env_truthy(key: str, *, default: bool = False) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


# Default OFF: no automatic ingest or agent runs — use POST /pipeline/* from the API or dashboard.
AUTO_PIPELINE = _env_truthy("CRISISFLOW_AUTO_PIPELINE", default=False)

app = Flask(__name__)
CORS(app)

# ── Shared state ──────────────────────────────────────────────────────────
# PriorityQueue: tuples of (-severity, monotonic_counter, event)
# Negative severity so that sev-5 sorts before sev-4, etc. (min-heap).
event_queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=1000)
_enqueue_counter: int = 0          # tie-break to preserve insertion order within same severity
processed_events: list[dict] = []  # ring buffer, last 500
quarantined_events: list[dict] = []# ring buffer, last 500
sse_subscribers: list[queue.Queue] = []    # one queue per SSE client
_lock = threading.Lock()

# Deduplication: track event IDs seen in the last 10 minutes
_seen_ids: dict[str, float] = {}
DEDUP_TTL = 600  # seconds
QUARANTINE_LIMIT = 500
PROCESSED_LIMIT = 500


# ── Agent singletons ──────────────────────────────────────────────────────
detection_agent    = DetectionAgent()
classification_agent = ClassificationAgent()
severity_agent     = SeverityAgent()
allocation_agent   = AllocationAgent()
communication_agent = CommunicationAgent()
consensus_engine   = ConsensusEngine()
coordinator_agent  = CoordinatorAgent()
debate_agent       = DebateAgent()
reflection_agent   = ReflectionAgent()
simulation_agent   = SimulationAgent()

# ── Claude Opus client (optional — degrades gracefully if key missing) ────
_claude = None
try:
    from claude_manager import ClaudeManager
    _claude = ClaudeManager()
    log.info("Claude AI enrichment: ENABLED")
except Exception as _claude_err:
    log.warning("Claude AI enrichment: DISABLED (%s)", _claude_err)


# ═══════════════════════════════════════════════════════════════════════════
# Agent runner
# ═══════════════════════════════════════════════════════════════════════════

AGENT_TIMEOUT     = 5.0   # seconds — rule-based agents
AI_AGENT_TIMEOUT  = 45.0  # seconds — LLM-backed agents (allocation needs extra time)


def _run_with_timeout(fn, *args, timeout=AGENT_TIMEOUT):
    """Run fn(*args) in a thread; return None on timeout or exception."""
    result_box = [None]
    exc_box = [None]

    def target():
        try:
            result_box[0] = fn(*args)
        except Exception as e:
            exc_box[0] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.error("Agent %s timed out after %.1fs", fn.__self__.__class__.__name__, timeout)
        return None
    if exc_box[0]:
        log.error("Agent %s raised: %s", fn.__self__.__class__.__name__, exc_box[0])
        return None
    return result_box[0]


def process_event(event: CrisisEvent) -> Optional[dict]:
    """
    Run a CrisisEvent through the full agent pipeline:
      Detection → Classification → Severity → Allocation → Consensus → Communication

    Returns the final enriched event as a dict, or None if rejected.
    """

    # ── Detection ────────────────────────────────────────────────────────
    if is_degraded("detection"):
        detection = None
    else:
        detection = _run_with_timeout(
            detection_agent.run, event, _claude,
            timeout=AI_AGENT_TIMEOUT if _claude else AGENT_TIMEOUT,
        )

    if detection is not None and not detection.accepted:
        log.info("Event %s REJECTED by detection: %s", event.id[:8], detection.reason)
        return None

    # ── Classification ───────────────────────────────────────────────────
    if is_degraded("classification"):
        classification = None
    else:
        classification = _run_with_timeout(
            classification_agent.run, event, _claude,
            timeout=AI_AGENT_TIMEOUT if _claude else AGENT_TIMEOUT,
        )

    # ── Severity (rule-based + optional Claude enrichment) ───────────────
    if is_degraded("severity"):
        severity = None
    else:
        sev_input = classification or type("_", (), {"event_type": event.type, "domain_tags": []})()
        _feedback_ctx = feedback_store.get_prompt_context(event.type) if _claude else ""
        severity = _run_with_timeout(
            severity_agent.run, event, sev_input, _claude, _feedback_ctx,
            timeout=AI_AGENT_TIMEOUT if _claude else AGENT_TIMEOUT,
        )

    # ── Debate: if adapter estimate and agent score diverge >= 2 ─────────
    debate_result = None
    if _claude is not None and severity is not None:
        spread = abs(severity.score - event.severity)
        if spread >= 2:
            debate_result = _run_with_timeout(
                debate_agent.run,
                event,
                event.severity, f"Initial {event.source} adapter estimate",
                severity.score, severity.reason,
                _claude,
                timeout=AI_AGENT_TIMEOUT,
            )
            if debate_result and debate_result.winning_score:
                severity.score = debate_result.winning_score
                severity.reason = (
                    f"[DEBATE:{debate_result.winning_position}] "
                    f"{debate_result.debate_summary} | {severity.reason}"
                )
                log.info(
                    "DEBATE resolved severity to %d (position=%s) for event %s",
                    debate_result.winning_score, debate_result.winning_position, event.id[:8],
                )

    # ── Allocation ───────────────────────────────────────────────────────
    if is_degraded("allocation"):
        allocation = None
    else:
        allocation = _run_with_timeout(
            allocation_agent.run, event, severity, _claude, _feedback_ctx,
            timeout=AI_AGENT_TIMEOUT if _claude else AGENT_TIMEOUT,
        )

    # ── Consensus ────────────────────────────────────────────────────────
    detection, classification, severity, allocation, flag = consensus_engine.resolve(
        event, detection, classification, severity, allocation
    )

    # ── Coordinator: final AI review of all results ───────────────────────
    coordinator_result = None
    if _claude is not None:
        coordinator_result = _run_with_timeout(
            coordinator_agent.run,
            event, classification, severity, allocation, _claude,
            timeout=AI_AGENT_TIMEOUT,
        )
        if coordinator_result and coordinator_result.severity_override is not None:
            log.info(
                "COORDINATOR overriding severity %d → %d: %s",
                severity.score if severity else "?",
                coordinator_result.severity_override,
                coordinator_result.reasoning,
            )
            if severity:
                severity.score = coordinator_result.severity_override
            event.severity = coordinator_result.severity_override

    # ── Collect per-agent reasoning for UI display (core agents) ────────────
    agent_reasoning: dict = {}
    for result_obj, name in [
        (detection, "detection"),
        (classification, "classification"),
        (severity, "severity"),
        (allocation, "allocation"),
    ]:
        if result_obj:
            agent_reasoning[name] = {
                "reason":     result_obj.reason,
                "confidence": getattr(result_obj, "confidence", 1.0),
                "reasoning":  getattr(result_obj, "reasoning", ""),
            }
    if debate_result:
        agent_reasoning["debate"] = {
            "winning_position": debate_result.winning_position,
            "winning_score":    debate_result.winning_score,
            "reasoning":        debate_result.debate_summary,
            "confidence":       debate_result.confidence,
        }
    if coordinator_result:
        agent_reasoning["coordinator"] = {
            "recommendation":         coordinator_result.final_recommendation,
            "inconsistency_detected": coordinator_result.inconsistency_detected,
            "reasoning":              coordinator_result.reasoning,
            "confidence":             coordinator_result.confidence,
        }

    # ── Apply resolved results to event ──────────────────────────────────
    if classification:
        event.type = classification.event_type
        event.domain_tags = classification.domain_tags
    if severity:
        event.severity = severity.score
    if allocation:
        event.allocation = {
            "resources":       allocation.resources,
            "eta_minutes":     allocation.eta_minutes,
            "depot_name":      allocation.depot_name,
            "depot_org":       allocation.depot_org,
            "depot_lat":       allocation.depot_lat,
            "depot_lon":       allocation.depot_lon,
            "transport_mode":  allocation.transport_mode,
            "convoys":         allocation.convoys,
            "need":            allocation.need,
            "total_committed": allocation.total_committed,
        }
    event.consensus_flag = flag

    # ── Communication ────────────────────────────────────────────────────
    if is_degraded("communication"):
        event.action_summary = f"[{event.type.upper()} sev={event.severity}] {event.title}"
        comm = None
    else:
        comm = _run_with_timeout(
            communication_agent.run,
            event, detection, classification, severity, allocation, flag, _claude,
            timeout=AI_AGENT_TIMEOUT if _claude else AGENT_TIMEOUT,
        )
        if comm:
            event.action_summary = comm.summary

    # ── Reflection: post-decision quality review ──────────────────────────
    reflection_result = None
    if _claude is not None:
        reflection_result = _run_with_timeout(
            reflection_agent.run,
            event, severity, allocation, coordinator_result, _claude,
            timeout=AI_AGENT_TIMEOUT,
        )

    # ── Simulation: escalation scenario projection ────────────────────────
    simulation_result = None
    if _claude is not None:
        simulation_result = _run_with_timeout(
            simulation_agent.run,
            event, severity, _claude,
            timeout=AI_AGENT_TIMEOUT,
        )

    # ── Record reflection into feedback loop for future events ───────────
    if reflection_result and reflection_result.assessment:
        feedback_store.record(
            event_type=event.type,
            assessment=reflection_result.assessment,
            concerns=reflection_result.concerns,
            suggestions=reflection_result.suggestions,
            confidence=reflection_result.confidence,
            location=event.location_name or "",
        )
        log.info(
            "Feedback recorded: type=%s assessment=%s",
            event.type, reflection_result.assessment,
        )

    # ── Add reflection + simulation to agent_reasoning ────────────────────
    if reflection_result:
        agent_reasoning["reflection"] = {
            "assessment":   reflection_result.assessment,
            "concerns":     reflection_result.concerns,
            "suggestions":  reflection_result.suggestions,
            "reasoning":    reflection_result.reasoning,
            "confidence":   reflection_result.confidence,
        }
    if simulation_result:
        agent_reasoning["simulation"] = {
            "projected_severity":            simulation_result.projected_severity,
            "projected_affected_population": simulation_result.projected_affected_population,
            "additional_resources":          simulation_result.additional_resources,
            "pre_position_hubs":             simulation_result.pre_position_hubs,
            "early_warning":                 simulation_result.early_warning,
            "reasoning":                     simulation_result.reasoning,
            "confidence":                    simulation_result.confidence,
        }
    event.agent_reasoning = agent_reasoning

    result = event.to_dict()
    if comm:
        result["globe_color"]          = comm.globe_color
        result["arc_source"]           = comm.arc_source
        result["arc_dest"]             = comm.arc_dest
        result["arcs"]                 = comm.arcs
        result["citizen_alert"]        = comm.citizen_alert
        result["operational_summary"]  = comm.operational_summary
    else:
        result["globe_color"] = "#888780"
        result["arc_source"]  = [0, 0]
        result["arc_dest"]    = [event.lat, event.lon]
        result["arcs"]        = []

    result = scrub_event_allocation_need_notes(result)

    log.info(
        "PROCESSED [%s] %s sev=%d flag=%s eta=%s",
        event.source.upper(), event.title[:60],
        event.severity, flag or "OK",
        f"{event.allocation.get('eta_minutes',0)}m",
    )
    return result


def _publish_processed_event(result: dict) -> None:
    """Append to ring buffer, notify SSE subscribers, persist to Snowflake."""
    with _lock:
        processed_events.append(result)
        if len(processed_events) > PROCESSED_LIMIT:
            processed_events.pop(0)
        dead = []
        for q in sse_subscribers:
            try:
                q.put_nowait(result)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_subscribers.remove(q)

    try:
        snowflake_store.store_event(result)
        convoys = (result.get("allocation") or {}).get("convoys") or []
        if convoys:
            snowflake_store.store_convoys(
                event_id=result.get("id", ""),
                event_type=result.get("type", "unknown"),
                event_lat=float(result.get("lat", 0)),
                event_lon=float(result.get("lon", 0)),
                convoys=convoys,
            )
    except Exception as exc:
        log.warning("Snowflake store failed (non-fatal): %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# Consumer thread — drains the queue and runs the pipeline
# ═══════════════════════════════════════════════════════════════════════════

def consumer_thread():
    while True:
        try:
            _priority, _counter, event = event_queue.get(timeout=1)
        except queue.Empty:
            continue

        result = process_event(event)
        if result is None:
            continue

        _publish_processed_event(result)


# ═══════════════════════════════════════════════════════════════════════════
# Ingestion threads — one per source
# ═══════════════════════════════════════════════════════════════════════════

def _enqueue(event: CrisisEvent):
    """Deduplicate by event ID and push to the shared queue."""
    def _coord_invalid_reason(ev: CrisisEvent) -> Optional[str]:
        lat = ev.lat
        lon = ev.lon
        if lat is None or lon is None:
            return "missing lat/lon"
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return "non-numeric lat/lon"
        if not math.isfinite(lat) or not math.isfinite(lon):
            return "non-finite lat/lon"
        if not (-90 <= lat <= 90):
            return "latitude out of range"
        if not (-180 <= lon <= 180):
            return "longitude out of range"
        if lat == 0 and lon == 0:
            return "placeholder coordinates (0,0)"
        return None

    def _quarantine(ev: CrisisEvent, reason: str):
        record = {
            "id": ev.id,
            "source": ev.source,
            "type": ev.type,
            "lat": ev.lat,
            "lon": ev.lon,
            "title": ev.title,
            "timestamp": ev.timestamp.isoformat(),
            "reason": reason,
        }
        with _lock:
            quarantined_events.append(record)
            if len(quarantined_events) > QUARANTINE_LIMIT:
                quarantined_events.pop(0)
        log.warning("Quarantined event %s from %s: %s", ev.id[:8], ev.source, reason)

    invalid_reason = _coord_invalid_reason(event)
    if invalid_reason:
        _quarantine(event, invalid_reason)
        return

    now = time.time()
    with _lock:
        # Evict stale IDs
        stale = [k for k, v in _seen_ids.items() if now - v > DEDUP_TTL]
        for k in stale:
            del _seen_ids[k]
        if event.id in _seen_ids:
            return
        _seen_ids[event.id] = now
    global _enqueue_counter
    try:
        # Priority = (-severity, counter) so higher severity processes first.
        # Counter breaks ties by preserving arrival order within the same severity.
        with _lock:
            _enqueue_counter += 1
            counter = _enqueue_counter
        event_queue.put_nowait((-event.severity, counter, event))
    except queue.Full:
        log.warning("Event queue full — dropping event from %s", event.source)


def poll_loop(adapter, interval_s: int):
    name = adapter.__class__.__name__
    while True:
        try:
            events = adapter.fetch()
            for e in events:
                _enqueue(e)
            log.debug("%s: enqueued %d events", name, len(events))
        except Exception as exc:
            log.error("%s poll error: %s", name, exc)
        time.sleep(interval_s)


def start_ingestion_threads():
    sources = [
        (USGSAdapter(),  120, "USGS"),
        (NOAAAdapter(),  300, "NOAA"),
        (GDACSAdapter(), 600, "GDACS"),
        (EONETAdapter(), 300, "EONET"),
    ]
    for adapter, interval, name in sources:
        t = threading.Thread(
            target=poll_loop, args=(adapter, interval),
            name=f"ingest-{name}", daemon=True,
        )
        t.start()
        log.info("Started ingestion thread: %s (every %ds)", name, interval)





# ═══════════════════════════════════════════════════════════════════════════
# Flask API
# ═══════════════════════════════════════════════════════════════════════════

def _dedup_events(events: list[dict]) -> list[dict]:
    """
    Remove duplicate events that share the same (title, date).
    Timestamp is truncated to date-only so that events polled multiple times
    on the same day (e.g. NOAA uses _utcnow()) are treated as identical.
    Keeps the first occurrence of each unique (title, date) pair.
    """
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for ev in events:
        title = (ev.get("title") or ev.get("TITLE") or "").strip()
        ts = ev.get("timestamp") or ev.get("TIMESTAMP") or ""
        # Normalise to date-only string: works for datetime objects and ISO strings
        date_str = str(ts)[:10]  # "YYYY-MM-DD"
        key = (title, date_str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    return deduped


@app.route("/events")
def get_events():
    """All events across all Snowflake tables, combined and deduplicated."""
    try:
        all_events = []
        for event_type in snowflake_store.TYPE_TO_TABLE:
            rows = snowflake_store.get_table(event_type, limit=300)
            for r in rows:
                r["type"] = event_type
            all_events.extend(rows)
        return jsonify([
            scrub_event_allocation_need_notes(e) for e in _dedup_events(all_events)
        ])
    except Exception:
        with _lock:
            return jsonify([
                scrub_event_allocation_need_notes(e) for e in _dedup_events(list(processed_events))
            ])


@app.route("/quarantine")
def get_quarantine():
    with _lock:
        return jsonify(list(quarantined_events))


@app.route("/inventory")
def get_inventory():
    """Current UNHRD depot inventory levels across all hubs."""
    import depot_inventory as _inv
    data = _inv.get_inventory()
    baselines = _inv.get_all_baselines()
    result = []
    for hub_name, stock in data.items():
        result.append({
            "hub_name":    hub_name,
            "stock":       stock,
            "baseline":    baselines.get(hub_name, {}),
            "stock_level": _inv.stock_level(hub_name),
        })
    return jsonify(result)


@app.route("/inventory/commit", methods=["POST"])
def inventory_commit():
    """Deduct hub stock. Used when ADK sets CRISISFLOW_INVENTORY_API_BASE to this API."""
    import depot_inventory as _inv

    data = request.get_json(silent=True) or {}
    hub = (data.get("hub_name") or "").strip()
    quantities = data.get("quantities") or {}
    if not hub or not isinstance(quantities, dict):
        return jsonify({"error": "hub_name and quantities object required"}), 400
    clean: dict[str, int] = {}
    for k, v in quantities.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            clean[str(k)] = n
    deducted = _inv.commit(hub, clean)
    return jsonify({"deducted": deducted})


@app.route("/pipeline/status", methods=["GET"])
def pipeline_status():
    """Whether automatic polling is enabled, and how many events wait in the queue."""
    return jsonify({
        "auto_pipeline": AUTO_PIPELINE,
        "queue_depth": event_queue.qsize(),
        "processed_buffer_len": len(processed_events),
    })


@app.route("/pipeline/poll-sources", methods=["POST"])
def pipeline_poll_sources():
    """
    One-shot fetch from USGS, NOAA, GDACS, EONET — enqueue deduplicated events.
    Use when CRISISFLOW_AUTO_PIPELINE is not set.
    """
    specs = [
        (USGSAdapter(), "USGS"),
        (NOAAAdapter(), "NOAA"),
        (GDACSAdapter(), "GDACS"),
        (EONETAdapter(), "EONET"),
    ]
    fetched_per_source: dict = {}
    for adapter, name in specs:
        try:
            evs = adapter.fetch()
            fetched_per_source[name] = len(evs)
            for e in evs:
                _enqueue(e)
        except Exception as exc:
            log.error("%s manual poll error: %s", name, exc)
            fetched_per_source[name] = f"error: {exc}"
    return jsonify({
        "ok": True,
        "fetched_per_source": fetched_per_source,
        "queue_depth": event_queue.qsize(),
    })


@app.route("/pipeline/process-next", methods=["POST"])
def pipeline_process_next():
    """
    Pop one event from the queue and run the full agent pipeline (may commit depot stock).
    """
    try:
        _priority, _counter, event = event_queue.get_nowait()
    except queue.Empty:
        return jsonify({
            "ok": True,
            "processed": False,
            "message": "Queue is empty — POST /pipeline/poll-sources first.",
            "queue_depth": 0,
        })

    result = process_event(event)
    if result is None:
        return jsonify({
            "ok": True,
            "processed": True,
            "accepted": False,
            "queue_depth": event_queue.qsize(),
        })

    _publish_processed_event(result)
    return jsonify({
        "ok": True,
        "processed": True,
        "accepted": True,
        "queue_depth": event_queue.qsize(),
        "event": result,
    })


@app.route("/stream")
def sse_stream():
    """Server-Sent Events endpoint — streams new events as they are processed."""
    client_q: queue.Queue = queue.Queue(maxsize=50)
    with _lock:
        sse_subscribers.append(client_q)

    def generate():
        # No initial snapshot — the frontend fetches via REST.
        # SSE only streams genuinely new events as they're processed.
        while True:
            try:
                ev = client_q.get(timeout=30)
                yield f"data: {json.dumps(ev)}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/snowflake/summary")
def sf_summary():
    """Row counts for every event-type table in Snowflake."""
    return jsonify(snowflake_store.get_all_tables_summary())


@app.route("/convoys")
def get_convoys():
    """Most recent convoy dispatch records from Snowflake."""
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify(snowflake_store.get_recent_convoys(limit))


@app.route("/distribute", methods=["POST"])
def distribute():
    """
    Run a week aid-distribution simulation on a caller-supplied event list.

    Body: JSON array of event objects (same shape as /events response).
    Returns: { committed, need, feed, events_served, events_unmet }
      committed  — {event_id: {resource: qty_committed}}
      need       — {event_id: {resource: qty_needed}}
      feed       — chronological list of allocation entries
    """
    from distribution_engine import simulate_week
    events = request.get_json(silent=True)
    if not isinstance(events, list):
        return jsonify({"error": "body must be a JSON array of events"}), 400
    result = simulate_week(events)
    return jsonify({
        "committed":      result.committed,
        "need":           result.need,
        "feed":           result.feed,
        "events_served":  result.events_served,
        "events_unmet":   result.events_unmet,
    })


@app.route("/snowflake/<event_type>")
def sf_by_type(event_type):
    """Query a specific event-type table. e.g. /snowflake/earthquake"""
    limit = min(int(request.args.get("limit", 50)), 100)
    return jsonify(snowflake_store.get_table(event_type, limit))


@app.route("/health")
def health():
    from consensus import _degraded_until, _failure_windows
    agents = ["detection", "classification", "severity", "allocation", "communication"]
    status = {}
    for a in agents:
        degraded = is_degraded(a)
        failures = len(_failure_windows.get(a, []))
        status[a] = {
            "status": "DEGRADED" if degraded else "OK",
            "recent_failures": failures,
        }
    return jsonify({
        "queue_depth": event_queue.qsize(),
        "processed_total": len(processed_events),
        "agents": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ═══════════════════════════════════════════════════════════════════════════
# A2A Agent Cards — served at /.well-known/agent.json per A2A protocol spec
# ═══════════════════════════════════════════════════════════════════════════

_ADK_DIR = os.path.join(os.path.dirname(__file__), "adk")

@app.route("/.well-known/agent.json")
def agent_card():
    """Serve the CrisisFlow agent card for A2A discovery."""
    card_path = os.path.join(_ADK_DIR, "crisisflow", "agent.json")
    with open(card_path) as f:
        return Response(f.read(), mimetype="application/json")

@app.route("/.well-known/logistics-agent.json")
def logistics_agent_card():
    """Serve the Logistics Specialist agent card for A2A discovery."""
    card_path = os.path.join(_ADK_DIR, "logistics_agent", "agent.json")
    with open(card_path) as f:
        return Response(f.read(), mimetype="application/json")


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("=== CrisisFlow pipeline starting ===")

    snowflake_store.drop_confidence_column()
    snowflake_store.ensure_ai_columns()
    snowflake_store.ensure_convoys_table()

    if AUTO_PIPELINE:
        threading.Thread(target=consumer_thread, name="consumer", daemon=True).start()
        start_ingestion_threads()
        log.info("Automatic pipeline ENABLED (CRISISFLOW_AUTO_PIPELINE)")
    else:
        log.info(
            "Automatic pipeline DISABLED — agents run only via POST /pipeline/poll-sources "
            "and POST /pipeline/process-next (or set CRISISFLOW_AUTO_PIPELINE=1)"
        )

    log.info("API server starting on http://0.0.0.0:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True, use_reloader=False)
