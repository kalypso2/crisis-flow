"""
CrisisFlow main pipeline — orchestrates ingestion, agent execution,
consensus resolution, and SSE streaming to the dashboard.

Start with:
    python pipeline/main.py

Exposes:
    GET  /events        — last 100 processed events (JSON)
    GET  /stream        — Server-Sent Events stream of new events
    GET  /health        — agent health / circuit-breaker status
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
from adapters import (
    USGSAdapter, NOAAAdapter, GDACSAdapter, EONETAdapter,
    ACLEDAdapter, TwitterAdapter,
)
from agents import (
    DetectionAgent, ClassificationAgent, SeverityAgent,
    AllocationAgent, CommunicationAgent,
)
from consensus import ConsensusEngine, is_degraded
import snowflake_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("pipeline.main")

app = Flask(__name__)
CORS(app)

# ── Shared state ──────────────────────────────────────────────────────────
event_queue: queue.Queue[CrisisEvent] = queue.Queue(maxsize=1000)
processed_events: list[dict] = []          # ring buffer, last 2000
quarantined_events: list[dict] = []        # ring buffer, last 500
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


# ═══════════════════════════════════════════════════════════════════════════
# Agent runner
# ═══════════════════════════════════════════════════════════════════════════

AGENT_TIMEOUT = 5.0  # seconds per agent


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
        detection = _run_with_timeout(detection_agent.run, event)

    if detection is not None and not detection.accepted:
        log.info("Event %s REJECTED by detection: %s", event.id[:8], detection.reason)
        return None

    # ── Classification ───────────────────────────────────────────────────
    if is_degraded("classification"):
        classification = None
    else:
        classification = _run_with_timeout(classification_agent.run, event)

    # ── Severity ─────────────────────────────────────────────────────────
    if is_degraded("severity"):
        severity = None
    else:
        sev_input = classification or type("_", (), {"event_type": event.type, "domain_tags": []})()
        severity = _run_with_timeout(severity_agent.run, event, sev_input)

    # ── Allocation ───────────────────────────────────────────────────────
    if is_degraded("allocation"):
        allocation = None
    else:
        allocation = _run_with_timeout(allocation_agent.run, event, severity)

    # ── Consensus ────────────────────────────────────────────────────────
    detection, classification, severity, allocation, flag = consensus_engine.resolve(
        event, detection, classification, severity, allocation
    )

    # ── Apply resolved results to event ──────────────────────────────────
    if classification:
        event.type = classification.event_type
        event.domain_tags = classification.domain_tags
    if severity:
        event.severity = severity.score
    if allocation:
        event.allocation = {
            "resources": allocation.resources,
            "eta_minutes": allocation.eta_minutes,
            "depot_name": allocation.depot_name,
            "depot_lat": allocation.depot_lat,
            "depot_lon": allocation.depot_lon,
        }
    event.consensus_flag = flag

    # ── Communication ────────────────────────────────────────────────────
    if is_degraded("communication"):
        event.action_summary = f"[{event.type.upper()} sev={event.severity}] {event.title}"
        comm = None
    else:
        comm = _run_with_timeout(
            communication_agent.run,
            event, detection, classification, severity, allocation, flag
        )
        if comm:
            event.action_summary = comm.summary

    result = event.to_dict()
    if comm:
        result["globe_color"] = comm.globe_color
        result["arc_source"] = comm.arc_source
        result["arc_dest"] = comm.arc_dest
    else:
        result["globe_color"] = "#888780"
        result["arc_source"] = [0, 0]
        result["arc_dest"] = [event.lat, event.lon]

    log.info(
        "PROCESSED [%s] %s sev=%d flag=%s eta=%s",
        event.source.upper(), event.title[:60],
        event.severity, flag or "OK",
        f"{event.allocation.get('eta_minutes',0)}m",
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Consumer thread — drains the queue and runs the pipeline
# ═══════════════════════════════════════════════════════════════════════════

def consumer_thread():
    while True:
        try:
            event = event_queue.get(timeout=1)
        except queue.Empty:
            continue

        result = process_event(event)
        if result is None:
            continue

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
        except Exception as exc:
            log.warning("Snowflake store failed (non-fatal): %s", exc)


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
    try:
        event_queue.put_nowait(event)
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
        (ACLEDAdapter(), 900, "ACLED"),
    ]
    for adapter, interval, name in sources:
        t = threading.Thread(
            target=poll_loop, args=(adapter, interval),
            name=f"ingest-{name}", daemon=True,
        )
        t.start()
        log.info("Started ingestion thread: %s (every %ds)", name, interval)

    # Twitter stream (persistent connection, not poll)
    twitter = TwitterAdapter()
    t = threading.Thread(
        target=twitter.stream, args=(_enqueue,),
        name="ingest-Twitter", daemon=True,
    )
    t.start()
    log.info("Started Twitter filtered stream thread")


def ingest_static_acled_once():
    """
    Load static ACLED markers and enqueue them through the full agent pipeline.

    Prefers data/acled_globe_markers_refined.json (timestamp-corrected by
    timestamp_agent.py) over the raw data/acled_globe_markers.json.
    Run `python3 timestamp_agent.py` once before starting the app to produce
    the refined file with accurate per-incident timestamps.
    """
    base = Path(__file__).resolve().parent / "data"
    refined_path  = base / "acled_globe_markers_refined.json"
    original_path = base / "acled_globe_markers.json"

    if refined_path.exists():
        path = refined_path
        log.info("ACLED static: using timestamp-refined file (%s)", refined_path.name)
    else:
        path = original_path
        log.info(
            "ACLED static: refined file not found — using original (%s). "
            "Run `python3 timestamp_agent.py` to generate accurate timestamps.",
            original_path.name,
        )

    if not path.exists():
        log.warning("ACLED static file missing: %s", path)
        return

    try:
        with path.open("r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as exc:
        log.error("Failed reading ACLED static data: %s", exc)
        return

    if not isinstance(rows, list):
        log.error("ACLED static payload is not a list: %s", path)
        return

    enqueued = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ts = row.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                ts = datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            event = CrisisEvent(
                id=str(row.get("id") or f"ACLED-STATIC-{enqueued}"),
                source="acled_static",
                type=str(row.get("type") or "conflict"),
                lat=float(row.get("lat") or 0.0),
                lon=float(row.get("lon") or 0.0),
                radius_km=float(row.get("radius_km") or 20.0),
                location_name=str(row.get("admin1") or row.get("country") or ""),
                severity=int(row.get("severity") or 3),
                affected_population=int(row.get("affected_population") or 0),
                timestamp=ts,
                status=str(row.get("status") or "active"),
                confidence=0.85,
                title=str(row.get("title") or "ACLED static conflict event"),
                raw=row,
            )
            _enqueue(event)
            enqueued += 1
        except Exception as exc:
            log.warning("Skipping malformed ACLED static row: %s", exc)

    log.info("ACLED static: enqueued %d events", enqueued)


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
        return jsonify(_dedup_events(all_events))
    except Exception:
        with _lock:
            return jsonify(_dedup_events(list(processed_events)))


@app.route("/quarantine")
def get_quarantine():
    with _lock:
        return jsonify(list(quarantined_events))


@app.route("/acled-static")
def get_acled_static():
    """Serve ACLED conflict zones from Snowflake CONFLICTS table."""
    try:
        return jsonify(snowflake_store.get_conflicts(min_severity=1, limit=300))
    except Exception as exc:
        log.error("Snowflake ACLED query failed, falling back to JSON: %s", exc)
        path = Path(__file__).resolve().parent / "data" / "acled_globe_markers.json"
        if not path.exists():
            return jsonify([])
        try:
            with path.open("r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception:
            return jsonify([])


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


@app.route("/snowflake/<event_type>")
def sf_by_type(event_type):
    """Query a specific event-type table. e.g. /snowflake/earthquake"""
    if event_type == "conflict":
        min_sev = int(request.args.get("min_severity", 1))
        limit = min(int(request.args.get("limit", 50)), 100)
        return jsonify(snowflake_store.get_conflicts(min_sev, limit))
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
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("=== CrisisFlow pipeline starting ===")

    threading.Thread(target=consumer_thread, name="consumer", daemon=True).start()
    start_ingestion_threads()

    log.info("API server starting on http://0.0.0.0:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True, use_reloader=False)
