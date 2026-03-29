#!/usr/bin/env python3
"""
CrisisFlow ADK Pipeline Runner — Weather/Disaster Enrichment

Iterates through disaster event types, running the full Disaster Analyst
pipeline for each with rotating Gemini API keys.

Usage:
    # Process a single event type
    python run_pipeline.py --type earthquake

    # Process all event types
    python run_pipeline.py

    # Limit to first N types (for testing)
    python run_pipeline.py --limit 2

    # Dry-run: just show what would be processed
    python run_pipeline.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

from gemini_manager import get_manager, preflight_check_keys

log = logging.getLogger("crisisflow.runner")

EVENT_TYPES = [
    "earthquake",
    "cyclone",
    "flood",
    "wildfire",
    "volcano",
    "storm",
    "drought",
]

RESULTS_DIR = ROOT / "data" / "briefings"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _event_counts() -> dict[str, int]:
    """Count unenriched events per type from Snowflake."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        import snowflake_store
        conn = snowflake_store._get_conn()
        cur = conn.cursor()
        counts = {}
        for etype, table in snowflake_store.TYPE_TO_TABLE.items():
            if etype == "conflict":
                continue
            try:
                cur.execute(f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE ENRICHMENT_STATUS IS NULL
                       OR ENRICHMENT_STATUS != 'complete'
                """)
                counts[etype] = cur.fetchone()[0]
            except Exception:
                counts[etype] = 0
        return counts
    except Exception as exc:
        log.warning("Could not query event counts: %s", exc)
        return {t: 0 for t in EVENT_TYPES}


async def run_pipeline(event_type: str) -> str | None:
    """Run the full Disaster Analyst pipeline for one event type."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from crisisflow import app as crisisflow_app

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=crisisflow_app.name,
        user_id="pipeline",
        state={
            "event_type": event_type,
            "event_data": "{}",
            "weather_analysis": "[]",
            "impact_analysis": "[]",
            "aid_context": "[]",
            "validation_result": "No validation run yet.",
            "correction_status": "",
            "enrichment_results": "No enrichment run yet.",
        },
    )

    runner = Runner(
        app=crisisflow_app,
        session_service=session_service,
    )

    message = types.Content(
        role="user",
        parts=[types.Part(
            text=f"Analyze and enrich {event_type} disaster events with "
                 f"weather context, impact assessment, and compound risk "
                 f"detection."
        )],
    )

    final_text = ""
    async for event in runner.run_async(
        user_id="pipeline",
        session_id=session.id,
        new_message=message,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text

    updated_session = await session_service.get_session(
        app_name=crisisflow_app.name,
        user_id="pipeline",
        session_id=session.id,
    )
    if updated_session and updated_session.state:
        st = updated_session.state
        log.info(
            "Session state after run — event_data len=%d, "
            "weather_analysis len=%d, impact_analysis len=%d, "
            "enrichment_results len=%d",
            len(str(st.get("event_data", ""))),
            len(str(st.get("weather_analysis", ""))),
            len(str(st.get("impact_analysis", ""))),
            len(str(st.get("enrichment_results", ""))),
        )

        state_briefing = st.get("final_briefing", "")
        if state_briefing and len(state_briefing) > len(final_text):
            final_text = state_briefing

    if final_text:
        out_path = RESULTS_DIR / f"{event_type}_briefing.txt"
        out_path.write_text(final_text, encoding="utf-8")
        log.info("Saved briefing to %s (%d chars)", out_path.name, len(final_text))

    return final_text


async def main(event_types: list[str], dry_run: bool = False):
    counts = _event_counts()
    manager = get_manager()

    available_keys = preflight_check_keys()

    from gemini_manager import PRIMARY_MODEL, FALLBACK_MODEL

    print(f"\nCrisisFlow ADK Pipeline — Disaster Enrichment")
    print(f"{'='*60}")
    print(f"API keys available: {available_keys}/{len(manager.keys)}")
    print(f"Primary model: {PRIMARY_MODEL}  |  Fallback: {FALLBACK_MODEL}")
    print(f"Event types to process: {len(event_types)}")

    if available_keys == 0:
        print("ERROR: All API keys are daily-quota exhausted. Try again later.")
        return
    print()

    for etype in event_types:
        unenriched = counts.get(etype, 0)
        print(f"  {etype}: {unenriched} unenriched events")
    print()

    if dry_run:
        print("DRY RUN — no API calls made.")
        return

    total_start = time.time()

    for i, etype in enumerate(event_types):
        unenriched = counts.get(etype, 0)
        if unenriched == 0:
            log.info("Skipping %s (no unenriched events)", etype)
            continue

        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(event_types)}] Processing: {etype} ({unenriched} unenriched)")
        print(f"{'='*60}")

        start = time.time()
        try:
            briefing = await run_pipeline(etype)
            elapsed = time.time() - start

            if briefing:
                print(f"\n--- Briefing ({elapsed:.1f}s) ---")
                print(briefing[:2000])
                if len(briefing) > 2000:
                    print(f"... ({len(briefing)} chars total)")
            else:
                print(f"No briefing generated ({elapsed:.1f}s)")

        except Exception as exc:
            elapsed = time.time() - start
            log.error(
                "Pipeline failed for %s after %.1fs: %s",
                etype, elapsed, exc, exc_info=True,
            )
            print(f"FAILED: {exc}")

    total_elapsed = time.time() - total_start
    usage = manager.get_usage_stats()
    total_calls = sum(u["calls"] for u in usage.values())

    print(f"\n{'='*60}")
    print(f"Pipeline complete in {total_elapsed:.1f}s")
    print(f"Total Gemini API calls: {total_calls}")
    print(f"Usage per key: {json.dumps(usage, indent=2)}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="CrisisFlow ADK Pipeline — Disaster Enrichment"
    )
    parser.add_argument("--type", help="Process a single event type")
    parser.add_argument("--limit", type=int, help="Limit to first N types")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show plan without running"
    )
    args = parser.parse_args()

    target_types = [args.type] if args.type else EVENT_TYPES
    if args.limit:
        target_types = target_types[:args.limit]

    asyncio.run(main(target_types, dry_run=args.dry_run))
