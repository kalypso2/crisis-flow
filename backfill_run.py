"""One-time historical backfill script — fills Jan 12 to Mar 22 2026 gap.

Run with:  python backfill_run.py
"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from datetime import datetime, timedelta, timezone
from adapters import USGSAdapter, GDACSAdapter, EONETAdapter
from schema import CrisisEvent
import snowflake_store


def crisis_to_dict(ev: CrisisEvent) -> dict:
    return {
        "id": ev.id,
        "type": ev.type,
        "source": ev.source,
        "title": ev.title,
        "lat": ev.lat,
        "lon": ev.lon,
        "radius_km": ev.radius_km,
        "location_name": ev.location_name,
        "severity": ev.severity,
        "affected_population": ev.affected_population,
        "timestamp": (
            ev.timestamp.isoformat()
            if ev.timestamp
            else datetime.now(timezone.utc).isoformat()
        ),
        "status": "active",
        "action_summary": "",
        "consensus_flag": "",
        "domain_tags": [],
        "allocation": {},
        "globe_color": "#888780",
        "arc_source": [0, 0],
        "arc_dest": [0, 0],
        "raw": ev.raw if isinstance(ev.raw, dict) else {},
    }


def main():
    usgs  = USGSAdapter()
    gdacs = GDACSAdapter()
    eonet = EONETAdapter()

    # Jan 5 and the current-week are already populated; fill the gap.
    START = datetime(2026, 1, 12, tzinfo=timezone.utc)
    END   = datetime(2026, 3, 23, tzinfo=timezone.utc)

    week_start = START
    total_stored = 0

    print(f"Backfill {START.date()} → {END.date()}", flush=True)

    while week_start < END:
        week_end = min(week_start + timedelta(days=7), END)
        ws = week_start.strftime("%Y-%m-%d")
        print(f"  Week {ws}", end=" ", flush=True)

        week_evs: list[CrisisEvent] = []
        for adapter, name in [(usgs, "USGS"), (gdacs, "GDACS"), (eonet, "EONET")]:
            try:
                fetched = adapter.fetch_week(week_start, week_end)
                week_evs.extend(fetched)
                print(f"{name}:{len(fetched)}", end=" ", flush=True)
            except Exception as exc:
                print(f"{name}:ERR({exc})", end=" ", flush=True)
            time.sleep(0.5)

        dicts = [crisis_to_dict(ev) for ev in week_evs]
        stored = snowflake_store.batch_store_events(dicts)
        total_stored += stored
        print(f"→ {stored}/{len(dicts)} stored", flush=True)
        week_start = week_end

    print(f"\nDone. Total stored: {total_stored}", flush=True)


if __name__ == "__main__":
    main()
