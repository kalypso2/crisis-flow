"""
ACLED Timestamp Refinement Agent
=================================
Uses Google Gemini with Search Grounding (via google-genai SDK) to find
accurate event dates for ACLED conflict markers. The markers in
acled_globe_markers.json have timestamps set to the weekly batch-upload
date — this agent searches for the actual date of the most significant
recent incident in each region.

This is architecturally equivalent to the Google ADK approach (which builds
on the same google-genai foundation) but without the 200+ package Google
Cloud dependency chain that ADK pulls in.

CRITICAL CONSTRAINTS:
  - Only enriches timestamps for EXISTING markers in acled_globe_markers.json
  - NEVER creates or invents new conflict events
  - NEVER modifies lat/lon, severity, title, or any field except timestamp fields
  - Falls back to midpoint of week_first/week_last when no news date is found

Usage:
    # Dry run — print prompts without making API calls
    python3 timestamp_agent.py --dry-run

    # Test with first 10 markers
    python3 timestamp_agent.py --limit 10

    # Process all 996 markers (~2.5 hours at 8 RPM)
    python3 timestamp_agent.py

    # Re-process cached markers (e.g. after tuning the prompt)
    python3 timestamp_agent.py --force

Output:
    data/acled_globe_markers_refined.json   loaded by main.py automatically
    data/timestamp_cache.json               per-marker cache, speeds re-runs

Setup:
    pip install google-genai python-dotenv
    # Add to .env (repo root):
    #   GOOGLE_API_KEY=your_key_here
    # Get a free key: https://aistudio.google.com/app/apikey
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("timestamp_agent")

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).resolve().parent
DATA_DIR      = ROOT / "data"
ORIGINAL_FILE = DATA_DIR / "acled_globe_markers.json"
REFINED_FILE  = DATA_DIR / "acled_globe_markers_refined.json"
CACHE_FILE    = DATA_DIR / "timestamp_cache.json"

# ── Rate limiting ─────────────────────────────────────────────────────────
# Gemini free tier: 10 RPM. Stay at 8 to avoid 429s.
REQUESTS_PER_MINUTE    = 8
DELAY_BETWEEN_REQUESTS = 60.0 / REQUESTS_PER_MINUTE   # 7.5 seconds
CHECKPOINT_EVERY       = 25   # save progress to disk every N markers

# ── Model ─────────────────────────────────────────────────────────────────
GEMINI_MODEL = "gemini-2.0-flash"


# ═══════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════

def _check_api_key() -> str:
    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        log.error(
            "GOOGLE_API_KEY not set.\n"
            "Add it to .env (repo root):\n"
            "  GOOGLE_API_KEY=your_key_here\n"
            "Get a free key at: https://aistudio.google.com/app/apikey"
        )
        sys.exit(1)
    return key


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Cache file corrupted — starting fresh")
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def midpoint_date(week_first: str, week_last: str) -> str:
    """
    Return an ISO timestamp midway between week_first and week_last.
    This is a much better fallback than always using week_last
    (the batch-upload date), because events could have occurred
    at any point in the observation window.
    """
    t1 = parse_iso(week_first)
    t2 = parse_iso(week_last)
    mid = t1 + (t2 - t1) / 2
    mid = mid.replace(hour=12, minute=0, second=0, microsecond=0)
    return mid.isoformat()


def is_within_window(date_str: str, week_first: str, week_last: str) -> bool:
    """Return True if date_str falls inside [week_first, week_last + 1 day]."""
    try:
        if "T" in date_str:
            d = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        else:
            d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        t1 = parse_iso(week_first)
        t2 = parse_iso(week_last) + timedelta(days=1)
        return t1 <= d <= t2
    except Exception:
        return False


def extract_date_from_text(text: str) -> str | None:
    """
    Regex fallback: extract a date from free text when the model
    doesn't return clean JSON.
    """
    # ISO format: 2026-03-07
    m = re.search(
        r"\b(20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))\b", text
    )
    if m:
        return m.group(1)

    # Verbose: "March 7, 2026" or "7 March 2026"
    months = (
        "January|February|March|April|May|June|"
        "July|August|September|October|November|December"
    )
    m = re.search(
        rf"\b({months})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b"
        rf"|\b(\d{{1,2}})\s+({months})\s+(20\d{{2}})\b",
        text, re.IGNORECASE,
    )
    if m:
        try:
            g = m.groups()
            if g[0]:
                return datetime.strptime(
                    f"{g[0]} {g[1]} {g[2]}", "%B %d %Y"
                ).strftime("%Y-%m-%d")
            return datetime.strptime(
                f"{g[3]} {g[4]} {g[5]}", "%d %B %Y"
            ).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Search prompt construction
# ═══════════════════════════════════════════════════════════════════════════

def build_search_prompt(row: dict) -> str:
    """
    Build a targeted search prompt.

    Goal: find the date of the most recent specific incident in this region
    from a real news source within the ACLED observation window.
    The prompt explicitly instructs the model not to invent dates or events.
    """
    country    = row["country"]
    admin1     = row["admin1"]
    sub_type   = (
        row.get("primary_sub_event_type")
        or row.get("primary_event_type")
        or "armed attack"
    )
    week_first = row["week_first"][:10]
    week_last  = row["week_last"][:10]
    fatalities = row.get("total_fatalities", 0)

    top_types = ", ".join(
        e["name"] for e in (row.get("event_type_breakdown") or [])[:2]
    ) or sub_type

    return f"""You are a conflict event researcher verifying timestamps for existing data records.

I have an existing conflict record for {admin1}, {country}. Its current timestamp
is the ACLED weekly batch-upload date ({week_last}), which is NOT the actual incident date.
I need to find when a specific incident in this region actually occurred.

Existing record context:
  Location         : {admin1}, {country}
  Primary type     : {sub_type}
  Other types      : {top_types}
  Total fatalities : {fatalities}
  Observation window: {week_first} to {week_last}

TASK: Use Google Search to find the most recent specific {sub_type.lower()} incident
in {admin1}, {country} that was reported by a reputable news source (Reuters, AP,
BBC, Al Jazeera, AFP, or similar) with an explicit date between {week_first} and {week_last}.

Return ONLY a single JSON object in exactly this format — no other text:
{{"date": "YYYY-MM-DD", "confidence": 0.85, "source_url": "https://...", "headline": "brief headline"}}

STRICT RULES:
- "date" must be a real calendar date from a search result, formatted YYYY-MM-DD
- The date must fall within the window {week_first} to {week_last}
- Use confidence > 0.7 only when a specific dated incident is explicitly confirmed
- If no specific dated incident is found, return:
  {{"date": null, "confidence": 0.0, "source_url": null, "headline": null}}
- DO NOT use {week_last} as the date unless a real article confirms an event on that exact date
- DO NOT invent or fabricate events, dates, or URLs
- This is timestamp enrichment only — DO NOT describe new events"""


# ═══════════════════════════════════════════════════════════════════════════
# Gemini Search Grounding agent
# ═══════════════════════════════════════════════════════════════════════════

def _make_client(api_key: str):
    """Create a google-genai Client."""
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except ImportError:
        log.error(
            "google-genai is not installed.\n"
            "Install it with:  pip install google-genai\n"
            "Then re-run this script."
        )
        sys.exit(1)


async def query_gemini_search(client, row: dict) -> dict:
    """
    Send one search-grounded prompt to Gemini and return a parsed result dict.

    Uses google-genai's built-in Google Search tool — the same mechanism that
    Google ADK's google_search tool wraps under the hood.
    """
    from google.genai import types

    prompt = build_search_prompt(row)

    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        response_modalities=["TEXT"],
        system_instruction=(
            "You are a conflict event timestamp researcher. "
            "Use Google Search to find real incident dates. "
            "Always return valid JSON. Never fabricate dates or events."
        ),
    )

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
        raw_text = response.text or ""
    except Exception as exc:
        log.error("Gemini API error for %s: %s", row.get("id", "?"), exc)
        return {"date": None, "confidence": 0.0, "source_url": None, "headline": None}

    return parse_agent_response(raw_text, row)


def parse_agent_response(text: str, row: dict) -> dict:
    """
    Extract date/confidence/source from the model's text response.
    Tries JSON parse first, falls back to regex date extraction.
    """
    result = {"date": None, "confidence": 0.0, "source_url": None, "headline": None}

    # Find and parse the JSON block the model should have returned
    json_match = re.search(r'\{[^{}]*"date"[^{}]*\}', text, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            for k in ("date", "confidence", "source_url", "headline"):
                if k in parsed:
                    result[k] = parsed[k]
        except json.JSONDecodeError:
            pass

    # If JSON parse failed or date is missing, try regex on raw text
    if not result.get("date"):
        found = extract_date_from_text(text)
        if found:
            result["date"]       = found
            result["confidence"] = 0.4   # lower confidence for regex-extracted dates

    # Validate: date must fall within the observation window
    if result.get("date"):
        week_first = row.get("week_first", "")
        week_last  = row.get("week_last", "")
        if week_first and week_last and not is_within_window(
            result["date"], week_first, week_last
        ):
            log.debug(
                "Date %s is outside window %s–%s for %s — discarding",
                result["date"], week_first[:10], week_last[:10], row.get("id", "?"),
            )
            result["date"]       = None
            result["confidence"] = 0.0

    result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0.0)))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# File I/O
# ═══════════════════════════════════════════════════════════════════════════

def _write_refined(original_markers: list[dict], refined_by_id: dict[str, dict]):
    """
    Write the refined output file preserving original marker order.
    Unprocessed markers get the midpoint heuristic timestamp so the file
    is always complete even mid-run.
    """
    output = []
    for row in original_markers:
        marker_id = row.get("id", "")
        if marker_id in refined_by_id:
            output.append(refined_by_id[marker_id])
        else:
            refined = deepcopy(row)
            refined["timestamp"]            = midpoint_date(row["week_first"], row["week_last"])
            refined["timestamp_refined"]    = False
            refined["timestamp_confidence"] = 0.0
            refined["timestamp_source_url"] = None
            refined["timestamp_headline"]   = None
            output.append(refined)

    REFINED_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Wrote %d markers to %s", len(output), REFINED_FILE.name)


# ═══════════════════════════════════════════════════════════════════════════
# Main refinement loop
# ═══════════════════════════════════════════════════════════════════════════

async def refine_timestamps(args):
    """Load markers → call Gemini Search per marker → save refined output."""
    api_key = None if args.dry_run else _check_api_key()

    if not ORIGINAL_FILE.exists():
        log.error("ACLED static file not found: %s", ORIGINAL_FILE)
        sys.exit(1)

    markers: list[dict] = json.loads(ORIGINAL_FILE.read_text(encoding="utf-8"))
    log.info("Loaded %d markers from %s", len(markers), ORIGINAL_FILE.name)

    # Load previous refined output so prior work is preserved across runs
    refined_by_id: dict[str, dict] = {}
    if REFINED_FILE.exists():
        try:
            prev = json.loads(REFINED_FILE.read_text(encoding="utf-8"))
            refined_by_id = {r["id"]: r for r in prev}
            log.info("Loaded %d previously refined markers", len(refined_by_id))
        except Exception:
            log.warning("Could not load previous refined file — starting fresh")

    cache = load_cache()
    log.info("Cache has %d entries", len(cache))

    work_markers = markers[: args.limit] if args.limit else markers
    log.info("Will process %d markers", len(work_markers))

    # ── Dry run: just show prompts ────────────────────────────────────────
    if args.dry_run:
        log.info("DRY RUN — showing first 3 prompts (no API calls)")
        for row in work_markers[:3]:
            print("\n" + "=" * 70)
            print(f"Marker: {row['id']}")
            print(build_search_prompt(row))
        return

    client = _make_client(api_key)
    stats  = {"searched": 0, "refined": 0, "cache_hit": 0, "fallback": 0, "errors": 0}

    for i, row in enumerate(work_markers):
        marker_id = row["id"]
        log.info("[%d/%d] %s", i + 1, len(work_markers), marker_id)

        refined = deepcopy(row)

        # ── Cache hit: skip API call ──────────────────────────────────────
        if not args.force and marker_id in cache:
            cached = cache[marker_id]
            refined["timestamp"]            = cached.get("refined_timestamp") or row["timestamp"]
            refined["timestamp_refined"]    = cached.get("date") is not None
            refined["timestamp_confidence"] = cached.get("confidence", 0.0)
            refined["timestamp_source_url"] = cached.get("source_url")
            refined["timestamp_headline"]   = cached.get("headline")
            refined_by_id[marker_id] = refined
            stats["cache_hit"] += 1
            log.info("  Cache hit → %s", refined["timestamp"][:10])
            continue

        # ── Live search ───────────────────────────────────────────────────
        try:
            result = await query_gemini_search(client, row)
            stats["searched"] += 1

            if result.get("date"):
                new_ts = f"{result['date']}T12:00:00+00:00"
                refined["timestamp"]            = new_ts
                refined["timestamp_refined"]    = True
                refined["timestamp_confidence"] = round(result.get("confidence", 0.7), 3)
                refined["timestamp_source_url"] = result.get("source_url")
                refined["timestamp_headline"]   = result.get("headline")
                stats["refined"] += 1
                log.info(
                    "  Refined → %s (conf=%.2f) %s",
                    result["date"],
                    refined["timestamp_confidence"],
                    (result.get("headline") or "")[:60],
                )
            else:
                # Fallback: midpoint of observation window
                mid = midpoint_date(row["week_first"], row["week_last"])
                refined["timestamp"]            = mid
                refined["timestamp_refined"]    = False
                refined["timestamp_confidence"] = 0.0
                refined["timestamp_source_url"] = None
                refined["timestamp_headline"]   = None
                stats["fallback"] += 1
                log.info("  No date found — midpoint fallback → %s", mid[:10])

            # Persist to cache
            cache[marker_id] = {
                "date":              result.get("date"),
                "confidence":        result.get("confidence", 0.0),
                "source_url":        result.get("source_url"),
                "headline":          result.get("headline"),
                "refined_timestamp": refined["timestamp"],
                "searched_at":       datetime.now(timezone.utc).isoformat(),
            }

        except Exception as exc:
            log.error("  Unhandled error on %s: %s", marker_id, exc)
            refined["timestamp_refined"]    = False
            refined["timestamp_confidence"] = 0.0
            stats["errors"] += 1

        refined_by_id[marker_id] = refined

        # ── Checkpoint every N markers ────────────────────────────────────
        if (i + 1) % CHECKPOINT_EVERY == 0:
            _write_refined(markers, refined_by_id)
            save_cache(cache)
            log.info(
                "  Checkpoint %d/%d — refined=%d cache_hit=%d fallback=%d errors=%d",
                i + 1, len(work_markers),
                stats["refined"], stats["cache_hit"], stats["fallback"], stats["errors"],
            )

        # ── Rate limit ────────────────────────────────────────────────────
        if i + 1 < len(work_markers):
            time.sleep(DELAY_BETWEEN_REQUESTS)

    # Final write
    _write_refined(markers, refined_by_id)
    save_cache(cache)

    log.info(
        "Done — searched=%d refined=%d cache_hit=%d fallback=%d errors=%d",
        stats["searched"], stats["refined"],
        stats["cache_hit"], stats["fallback"], stats["errors"],
    )
    log.info("Output : %s", REFINED_FILE)
    log.info("Cache  : %s", CACHE_FILE)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Refine ACLED conflict timestamps using Gemini + Google Search Grounding"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only first N markers (default: all 996)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process markers already present in the cache",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print search prompts without making any API calls",
    )
    args = parser.parse_args()
    asyncio.run(refine_timestamps(args))


if __name__ == "__main__":
    main()
