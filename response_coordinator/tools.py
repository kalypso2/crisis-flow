"""
Response Coordinator — Custom tools for action planning and alert generation.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("crisisflow.response_coordinator")

DEPOTS = [
    {"name": "Geneva Hub", "lat": 46.20, "lon": 6.15},
    {"name": "Nairobi Hub", "lat": -1.29, "lon": 36.82},
    {"name": "Bangkok Hub", "lat": 13.76, "lon": 100.50},
    {"name": "Mexico City Hub", "lat": 19.43, "lon": -99.13},
    {"name": "Sydney Hub", "lat": -33.87, "lon": 151.21},
    {"name": "Dubai Hub", "lat": 25.20, "lon": 55.27},
]

ALERTS_DIR = ROOT / "data" / "alerts"
ALERTS_DIR.mkdir(parents=True, exist_ok=True)


def generate_alert_payload(alerts_json: str) -> str:
    """Generate structured alert payloads and save them to disk.

    Args:
        alerts_json: JSON array of alert objects, each with zone_id,
            alert_level (red/orange/yellow), action_required (string),
            eta_hours (number), and resources (list of strings).
    """
    try:
        alerts = json.loads(alerts_json)
    except json.JSONDecodeError:
        return "ERROR: invalid JSON for alerts"

    out_path = ALERTS_DIR / "latest_alerts.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(alerts, f, indent=2)

    red_count = sum(1 for a in alerts if a.get("alert_level") == "red")
    orange_count = sum(1 for a in alerts if a.get("alert_level") == "orange")

    log.info("Generated %d alerts (%d red, %d orange)", len(alerts), red_count, orange_count)
    return (
        f"Generated {len(alerts)} alert payloads ({red_count} red, {orange_count} orange). "
        f"Saved to {out_path.name}."
    )


def update_response_database(response_plan_json: str) -> str:
    """Save the full response plan to the database and disk.

    Args:
        response_plan_json: JSON object containing the full response plan
            with priority_actions, resource_allocation, alert_payloads,
            and coordination_notes.
    """
    try:
        plan = json.loads(response_plan_json)
    except json.JSONDecodeError:
        return "ERROR: invalid JSON for response plan"

    out_path = ALERTS_DIR / "latest_response_plan.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)

    try:
        import snowflake_store
        conn = snowflake_store._get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS RESPONSE_PLANS (
                id VARCHAR DEFAULT UUID_STRING(),
                plan VARIANT,
                created_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
            )
        """)
        cur.execute(
            "INSERT INTO RESPONSE_PLANS (plan) SELECT PARSE_JSON(%s)",
            (json.dumps(plan),),
        )
        conn.commit()
        return f"Response plan saved to Snowflake and {out_path.name}."
    except Exception as exc:
        log.warning("Snowflake save failed (plan saved locally): %s", exc)
        return f"Response plan saved locally to {out_path.name}."
