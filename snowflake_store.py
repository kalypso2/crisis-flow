"""
Snowflake persistence layer for CrisisFlow.

One table per event type:
  CONFLICTS, EARTHQUAKES, FLOODS, STORMS, WILDFIRES,
  CYCLONES, VOLCANOES, DROUGHTS, ICEBERGS

The CONFLICTS table has extra columns for ACLED zone-level data
and agent enrichment fields.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import snowflake.connector

log = logging.getLogger("snowflake_store")

_pool_lock = threading.Lock()
_conn: Optional[snowflake.connector.SnowflakeConnection] = None

TYPE_TO_TABLE = {
    "conflict":   "CONFLICTS",
    "earthquake": "EARTHQUAKES",
    "flood":      "FLOODS",
    "storm":      "STORMS",
    "wildfire":   "WILDFIRES",
    "cyclone":    "CYCLONES",
    "volcano":    "VOLCANOES",
    "drought":    "DROUGHTS",
    "iceberg":    "ICEBERGS",
}

ALL_TABLES = list(TYPE_TO_TABLE.values())
MAX_PER_TABLE = 300


def _get_conn() -> snowflake.connector.SnowflakeConnection:
    global _conn
    with _pool_lock:
        if _conn is None or _conn.is_closed():
            _conn = snowflake.connector.connect(
                account=os.environ.get("SNOWFLAKE_ACCOUNT", ""),
                user=os.environ.get("SNOWFLAKE_USER", ""),
                password=os.environ.get("SNOWFLAKE_PASSWORD", ""),
                database=os.environ.get("SNOWFLAKE_DATABASE", "CRISISFLOW"),
                schema=os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
                warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
            )
            log.info("Snowflake connection established")
        return _conn


# ── Per-table row cap tracking ─────────────────────────────────────────────
_table_counts: dict[str, int] = {}
_counts_loaded = False


def _load_table_counts():
    global _counts_loaded
    if _counts_loaded:
        return
    try:
        conn = _get_conn()
        cur = conn.cursor()
        for tbl in ALL_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            _table_counts[tbl] = cur.fetchone()[0]
        _counts_loaded = True
        log.info("Table counts: %s", _table_counts)
    except Exception as exc:
        log.warning("Could not load table counts: %s", exc)


def _evict_oldest(table: str):
    """Delete the oldest row to make room when table is at cap."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(f"""
        DELETE FROM {table}
        WHERE id = (
            SELECT id FROM {table}
            ORDER BY ingested_at ASC LIMIT 1
        )
    """)
    conn.commit()
    _table_counts[table] = max(0, _table_counts.get(table, 1) - 1)


# ── Store a processed event into its type-specific table ───────────────────

_BASE_MERGE = """
MERGE INTO {table} tgt USING (SELECT %s AS id) src ON tgt.id = src.id
WHEN NOT MATCHED THEN INSERT (
    id, source, title, lat, lon, radius_km, location_name,
    severity, affected_population, timestamp, status,
    action_summary, consensus_flag, domain_tags, allocation,
    globe_color, arc_source, arc_dest, raw
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, PARSE_JSON(%s), PARSE_JSON(%s),
    %s, PARSE_JSON(%s), PARSE_JSON(%s), PARSE_JSON(%s)
)
"""


def _base_params(ev: dict[str, Any]) -> tuple:
    eid = ev.get("id", "")
    return (
        eid,  # MERGE key
        eid,
        ev.get("source", ""),
        ev.get("title", ""),
        float(ev.get("lat", 0)),
        float(ev.get("lon", 0)),
        float(ev.get("radius_km", 0)),
        ev.get("location_name", ""),
        int(ev.get("severity", 1)),
        int(ev.get("affected_population", 0)),
        ev.get("timestamp", datetime.now(timezone.utc).isoformat()),
        ev.get("status", "active"),
        ev.get("action_summary", ""),
        ev.get("consensus_flag", ""),
        json.dumps(ev.get("domain_tags", [])),
        json.dumps(ev.get("allocation", {})),
        ev.get("globe_color", "#888780"),
        json.dumps(ev.get("arc_source", [0, 0])),
        json.dumps(ev.get("arc_dest", [0, 0])),
        json.dumps(ev.get("raw", {})),
    )


def store_event(ev: dict[str, Any]) -> bool:
    event_type = ev.get("type", "unknown")
    table = TYPE_TO_TABLE.get(event_type)
    if not table:
        log.debug("No table for event type '%s', skipping", event_type)
        return False

    _load_table_counts()
    if _table_counts.get(table, 0) >= MAX_PER_TABLE:
        _evict_oldest(table)

    try:
        conn = _get_conn()
        cur = conn.cursor()
        sql = _BASE_MERGE.format(table=table)
        cur.execute(sql, _base_params(ev))
        conn.commit()
        _table_counts[table] = _table_counts.get(table, 0) + 1
        return True
    except Exception as exc:
        log.error("Failed to store event %s in %s: %s",
                  ev.get("id", "?")[:8], table, exc)
        return False


# ── Load ACLED conflict zones (bulk, with extra columns) ──────────────────

_CONFLICT_MERGE = """
MERGE INTO CONFLICTS tgt USING (SELECT %s AS id) src ON tgt.id = src.id
WHEN NOT MATCHED THEN INSERT (
    id, source, title, lat, lon, radius_km, location_name,
    severity, affected_population, timestamp, status,
    action_summary, consensus_flag, domain_tags, allocation,
    globe_color, arc_source, arc_dest, raw,
    country, admin1, total_events, total_fatalities, population_exposed,
    alert_level, week_first, week_last, active_weeks,
    event_type_breakdown, sub_event_type_breakdown, disorder_type_breakdown
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, PARSE_JSON(%s), PARSE_JSON(%s),
    %s, PARSE_JSON(%s), PARSE_JSON(%s), PARSE_JSON(%s),
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    PARSE_JSON(%s), PARSE_JSON(%s), PARSE_JSON(%s)
)
"""


def store_conflict_zone(m: dict[str, Any]) -> bool:
    """Store an ACLED aggregated conflict zone into CONFLICTS."""
    ts = m.get("timestamp", datetime.now(timezone.utc).isoformat())
    eid = str(m.get("id", ""))
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(_CONFLICT_MERGE, (
            eid,  # MERGE key
            eid,
            "acled",
            str(m.get("title", "")),
            float(m.get("lat", 0)),
            float(m.get("lon", 0)),
            float(m.get("radius_km", 20)),
            str(m.get("admin1") or m.get("country") or ""),
            int(m.get("severity", 1)),
            int(m.get("affected_population") or m.get("population_exposed") or 0),
            ts,
            str(m.get("status", "active")),
            "",
            "",
            json.dumps([]),
            json.dumps({}),
            "#E24B4A",
            json.dumps([0, 0]),
            json.dumps([float(m.get("lat", 0)), float(m.get("lon", 0))]),
            json.dumps(m),
            str(m.get("country", "")),
            str(m.get("admin1", "")),
            int(m.get("total_events", 0)),
            int(m.get("total_fatalities", 0)),
            int(m.get("population_exposed") or m.get("affected_population") or 0),
            str(m.get("alert_level", "")),
            str(m.get("week_first", "")),
            str(m.get("week_last", "")),
            int(m.get("active_weeks", 0)),
            json.dumps(m.get("event_type_breakdown", [])),
            json.dumps(m.get("sub_event_type_breakdown", [])),
            json.dumps(m.get("disorder_type_breakdown", [])),
        ))
        conn.commit()
        return True
    except Exception as exc:
        log.error("Failed to store conflict zone %s: %s", eid[:8], exc)
        return False


# ── Store agent enrichment on an existing conflict row ─────────────────────

def store_enrichment(zone_id: str, intel: dict[str, Any]) -> bool:
    """Update a CONFLICTS row with agent-enriched intelligence."""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE CONFLICTS SET
                enrichment_status = %s,
                enrichment_summary = %s,
                recent_events = PARSE_JSON(%s),
                displaced_population = %s,
                humanitarian_access = %s,
                key_actors = PARSE_JSON(%s),
                trend = %s,
                enriched_at = CURRENT_TIMESTAMP()
            WHERE id = %s
        """, (
            intel.get("status", ""),
            intel.get("summary", ""),
            json.dumps(intel.get("recent_events", [])),
            int(intel.get("displaced_population", 0)),
            intel.get("humanitarian_access", ""),
            json.dumps(intel.get("key_actors", [])),
            intel.get("trend", ""),
            zone_id,
        ))
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        log.error("store_enrichment failed for %s: %s", zone_id, exc)
        return False


# ── Query helpers ──────────────────────────────────────────────────────────

def get_table(event_type: str, limit: int = 50) -> list[dict]:
    table = TYPE_TO_TABLE.get(event_type)
    if not table:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor(snowflake.connector.DictCursor)
        cur.execute(f"""
            SELECT * FROM {table}
            ORDER BY severity DESC, timestamp DESC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        log.error("get_table(%s) failed: %s", table, exc)
        return []


def get_all_tables_summary() -> dict[str, int]:
    """Row counts for every event-type table."""
    summary: dict[str, int] = {}
    try:
        conn = _get_conn()
        cur = conn.cursor()
        for event_type, table in TYPE_TO_TABLE.items():
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            summary[event_type] = cur.fetchone()[0]
    except Exception as exc:
        log.error("get_all_tables_summary failed: %s", exc)
    return summary


def get_conflicts(min_severity: int = 1, limit: int = 50) -> list[dict]:
    """Fetch conflict zones with ACLED + enrichment data."""
    try:
        conn = _get_conn()
        cur = conn.cursor(snowflake.connector.DictCursor)
        cur.execute("""
            SELECT * FROM CONFLICTS
            WHERE severity >= %s
            ORDER BY total_fatalities DESC NULLS LAST, severity DESC
            LIMIT %s
        """, (min_severity, limit))
        rows = cur.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            for col in ("EVENT_TYPE_BREAKDOWN", "SUB_EVENT_TYPE_BREAKDOWN",
                        "DISORDER_TYPE_BREAKDOWN", "RECENT_EVENTS",
                        "KEY_ACTORS", "DOMAIN_TAGS", "ALLOCATION",
                        "ARC_SOURCE", "ARC_DEST", "RAW"):
                if isinstance(d.get(col), str):
                    d[col] = json.loads(d[col])
            results.append(d)
        return results
    except Exception as exc:
        log.error("get_conflicts failed: %s", exc)
        return []
