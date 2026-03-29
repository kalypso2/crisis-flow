"""
Snowflake persistence layer for CrisisFlow.

One table per event type:
  EARTHQUAKES, FLOODS, STORMS, WILDFIRES,
  CYCLONES, VOLCANOES, DROUGHTS, ICEBERGS

Plus a CONVOYS table for multi-hub aid allocation records.
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


# ── One-time schema migration ─────────────────────────────────────────────

def ensure_ai_columns() -> None:
    """
    Add agent_reasoning (VARIANT), citizen_alert (TEXT), and
    operational_summary (TEXT) to all event tables if they don't exist.
    Safe to call on every startup.
    """
    new_cols = [
        ("AGENT_REASONING",    "VARIANT"),
        ("CITIZEN_ALERT",      "TEXT"),
        ("OPERATIONAL_SUMMARY","TEXT"),
        ("ARCS",               "VARIANT"),
    ]
    try:
        conn = _get_conn()
        cur = conn.cursor()
        for table in ALL_TABLES:
            for col, col_type in new_cols:
                try:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    conn.commit()
                except Exception:
                    pass  # column already exists or table not yet created
        log.info("AI columns ensured on all event tables")
    except Exception as exc:
        log.warning("ensure_ai_columns: skipped (%s)", exc)


def drop_confidence_column() -> None:
    """
    Drop the CONFIDENCE column from all event tables if it still exists.
    Safe to call on every startup — silently skips tables that already
    had the column removed.
    """
    try:
        conn = _get_conn()
        cur = conn.cursor()
        for table in ALL_TABLES:
            try:
                cur.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS CONFIDENCE")
                conn.commit()
                log.info("Migration: dropped CONFIDENCE column from %s (or it was already gone)", table)
            except Exception as exc:
                log.warning("Migration: could not drop CONFIDENCE from %s: %s", table, exc)
    except Exception as exc:
        log.warning("Migration: skipped (Snowflake unavailable): %s", exc)


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
WHEN MATCHED THEN UPDATE SET
    severity = %s, affected_population = %s, status = %s,
    action_summary = %s, consensus_flag = %s,
    domain_tags = PARSE_JSON(%s), allocation = PARSE_JSON(%s),
    globe_color = %s, arc_source = PARSE_JSON(%s), arc_dest = PARSE_JSON(%s),
    raw = PARSE_JSON(%s), agent_reasoning = PARSE_JSON(%s),
    citizen_alert = %s, operational_summary = %s, arcs = PARSE_JSON(%s)
WHEN NOT MATCHED THEN INSERT (
    id, source, title, lat, lon, radius_km, location_name,
    severity, affected_population, timestamp, status,
    action_summary, consensus_flag, domain_tags, allocation,
    globe_color, arc_source, arc_dest, raw,
    agent_reasoning, citizen_alert, operational_summary, arcs
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, PARSE_JSON(%s), PARSE_JSON(%s),
    %s, PARSE_JSON(%s), PARSE_JSON(%s), PARSE_JSON(%s),
    PARSE_JSON(%s), %s, %s, PARSE_JSON(%s)
)
"""


def _base_params(ev: dict[str, Any]) -> tuple:
    eid = ev.get("id", "")
    # Shared field values
    severity          = int(ev.get("severity", 1))
    affected_pop      = int(ev.get("affected_population", 0))
    status            = ev.get("status", "active")
    action_summary    = ev.get("action_summary", "")
    consensus_flag    = ev.get("consensus_flag", "")
    domain_tags       = json.dumps(ev.get("domain_tags", []))
    allocation        = json.dumps(ev.get("allocation", {}))
    globe_color       = ev.get("globe_color", "#888780")
    arc_source        = json.dumps(ev.get("arc_source", [0, 0]))
    arc_dest          = json.dumps(ev.get("arc_dest", [0, 0]))
    raw               = json.dumps(ev.get("raw", {}))
    agent_reasoning   = json.dumps(ev.get("agent_reasoning", {}))
    citizen_alert     = ev.get("citizen_alert", "") or ""
    operational_sum   = ev.get("operational_summary", "") or ""
    arcs              = json.dumps(ev.get("arcs", []))
    return (
        eid,  # MERGE key
        # UPDATE fields (WHEN MATCHED)
        severity, affected_pop, status,
        action_summary, consensus_flag,
        domain_tags, allocation,
        globe_color, arc_source, arc_dest,
        raw, agent_reasoning,
        citizen_alert, operational_sum, arcs,
        # INSERT fields (WHEN NOT MATCHED)
        eid,
        ev.get("source", ""),
        ev.get("title", ""),
        float(ev.get("lat", 0)),
        float(ev.get("lon", 0)),
        float(ev.get("radius_km", 0)),
        ev.get("location_name", ""),
        severity, affected_pop,
        ev.get("timestamp", datetime.now(timezone.utc).isoformat()),
        status,
        action_summary, consensus_flag, domain_tags, allocation,
        globe_color, arc_source, arc_dest, raw,
        agent_reasoning, citizen_alert, operational_sum, arcs,
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


# ── CONVOYS table ──────────────────────────────────────────────────────────

_CREATE_CONVOYS = """
CREATE TABLE IF NOT EXISTS CONVOYS (
    id            VARCHAR(64) PRIMARY KEY,
    event_id      VARCHAR(64),
    event_type    VARCHAR(32),
    hub_name      VARCHAR(128),
    hub_org       VARCHAR(64),
    hub_lat       FLOAT,
    hub_lon       FLOAT,
    event_lat     FLOAT,
    event_lon     FLOAT,
    transport     VARCHAR(16),
    dist_km       FLOAT,
    eta_minutes   INT,
    supplies      VARIANT,
    dispatched_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
)
"""

_CONVOY_MERGE = """
MERGE INTO CONVOYS tgt USING (SELECT %s AS id) src ON tgt.id = src.id
WHEN NOT MATCHED THEN INSERT (
    id, event_id, event_type,
    hub_name, hub_org, hub_lat, hub_lon,
    event_lat, event_lon,
    transport, dist_km, eta_minutes, supplies
) VALUES (
    %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s, PARSE_JSON(%s)
)
"""


def ensure_convoys_table() -> None:
    """Create CONVOYS table if it doesn't exist yet."""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(_CREATE_CONVOYS)
        conn.commit()
        log.info("CONVOYS table ready")
    except Exception as exc:
        log.warning("Could not create CONVOYS table: %s", exc)


def store_convoys(event_id: str, event_type: str,
                  event_lat: float, event_lon: float,
                  convoys: list[dict]) -> int:
    """
    Persist each convoy leg for a processed event.
    Returns the number of rows inserted.
    """
    if not convoys:
        return 0
    stored = 0
    try:
        conn = _get_conn()
        cur  = conn.cursor()
        for c in convoys:
            cid = f"{event_id}_{c.get('hub_name','?').replace(' ','_')}"
            cur.execute(_CONVOY_MERGE, (
                cid,  # MERGE key
                cid,
                event_id,
                event_type,
                c.get("hub_name", ""),
                c.get("hub_org",  ""),
                float(c.get("hub_lat", 0)),
                float(c.get("hub_lon", 0)),
                event_lat,
                event_lon,
                c.get("transport", "air"),
                float(c.get("dist_km", 0)),
                int(c.get("eta_minutes", 0)),
                json.dumps(c.get("supplies", {})),
            ))
            stored += 1
        conn.commit()
    except Exception as exc:
        log.error("store_convoys failed for event %s: %s", event_id, exc)
    return stored


def get_recent_convoys(limit: int = 50) -> list[dict]:
    """Fetch the most recently dispatched convoy records."""
    try:
        conn = _get_conn()
        cur  = conn.cursor(snowflake.connector.DictCursor)
        cur.execute("""
            SELECT * FROM CONVOYS
            ORDER BY dispatched_at DESC
            LIMIT %s
        """, (limit,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if isinstance(d.get("SUPPLIES"), str):
                d["SUPPLIES"] = json.loads(d["SUPPLIES"])
            rows.append(d)
        return rows
    except Exception as exc:
        log.error("get_recent_convoys failed: %s", exc)
        return []
