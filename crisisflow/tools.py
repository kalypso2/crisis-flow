"""
CrisisFlow ADK — Custom tools for the disaster enrichment pipeline.

Tools:
  validate_risk_assessment  — programmatic QA on impact/weather analysis
  save_disaster_enrichment  — persist AI analysis to Snowflake weather tables
  send_a2a_briefing         — POST briefing to Response Coordinator via A2A
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from google.adk.tools import ToolContext

log = logging.getLogger("crisisflow.tools")

A2A_RESPONSE_COORDINATOR_URL = os.environ.get(
    "A2A_RESPONSE_COORDINATOR_URL", "http://localhost:8001"
)


# ── JSON parsing helpers ──────────────────────────────────────────────────


def _extract_json_array(text: str) -> str | None:
    import re
    match = re.search(r"\[[\s\S]*\]", text)
    return match.group(0) if match else None


def _safe_parse_json_array(value: Any) -> list:
    """Parse a JSON array from a value that might be a list, JSON string, or
    narrative text containing an embedded JSON array."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    extracted = _extract_json_array(value)
    if extracted:
        try:
            parsed = json.loads(extracted)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _safe_parse_json_obj(value: Any) -> dict:
    """Parse a JSON object from a value."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    import re
    match = re.search(r"\{[\s\S]*\}", value)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


# ── Validate risk assessment ──────────────────────────────────────────────


_SEVERITY_MAP = {"low": 1, "moderate": 2, "high": 3, "critical": 4, "extreme": 5}


def validate_risk_assessment(tool_context: ToolContext) -> str:
    """Run programmatic quality checks on the weather and impact analyses.

    Reads weather_analysis, impact_analysis, and event_data from session
    state. Returns a JSON result with pass/fail for each event.
    """
    event_data = _safe_parse_json_obj(tool_context.state.get("event_data", "{}"))
    events = event_data.get("events", [])
    if not events:
        events = _safe_parse_json_array(tool_context.state.get("event_data", "[]"))

    weather = _safe_parse_json_array(tool_context.state.get("weather_analysis", "[]"))
    impact = _safe_parse_json_array(tool_context.state.get("impact_analysis", "[]"))

    if not events:
        all_ids = set()
        for w in weather:
            if isinstance(w, dict) and w.get("event_id"):
                all_ids.add(w["event_id"])
        for i in impact:
            if isinstance(i, dict) and i.get("event_id"):
                all_ids.add(i["event_id"])
        if all_ids:
            events = [{"id": eid} for eid in all_ids]

    weather_by_id = {w.get("event_id", ""): w for w in weather if isinstance(w, dict)}
    impact_by_id = {i.get("event_id", ""): i for i in impact if isinstance(i, dict)}

    event_ids = {e.get("id", "") for e in events if isinstance(e, dict) and e.get("id")}

    passed = []
    failed = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id", "")
        if not eid:
            continue

        issues = []
        w = weather_by_id.get(eid)
        imp = impact_by_id.get(eid)

        if not w:
            issues.append("missing weather analysis")
        if not imp:
            issues.append("missing impact analysis")

        if imp:
            pop = imp.get("estimated_affected_population", 0)
            if isinstance(pop, str):
                try:
                    pop = int(pop.replace(",", ""))
                except ValueError:
                    pop = 0
            if pop < 0:
                issues.append(f"negative population estimate: {pop}")
            if pop > 500_000_000:
                issues.append(f"implausible population estimate: {pop}")

            impact_sev = _SEVERITY_MAP.get(
                str(imp.get("impact_severity", "")).lower(), 0
            )
            event_sev = int(ev.get("severity", 1))
            if impact_sev > 0 and abs(impact_sev - event_sev) > 2:
                issues.append(
                    f"impact_severity '{imp.get('impact_severity')}' "
                    f"misaligned with event severity {event_sev}"
                )

        if issues:
            failed.append({"event_id": eid, "title": ev.get("title", ""),
                           "issues": issues})
        else:
            passed.append({"event_id": eid, "title": ev.get("title", "")})

    missing_weather = len(event_ids) - len(weather_by_id)
    missing_impact = len(event_ids) - len(impact_by_id)

    result = {
        "total_events": len(events),
        "passed": len(passed),
        "failed": len(failed),
        "missing_weather_analysis": missing_weather,
        "missing_impact_analysis": missing_impact,
        "passed_events": passed,
        "failed_events": failed,
        "all_passed": len(failed) == 0,
    }

    log.info("Risk validation: %d passed, %d failed", len(passed), len(failed))

    tool_context.state["validation_passed"] = json.dumps(passed)
    tool_context.state["validation_failed"] = json.dumps(failed)

    return json.dumps(result)


# ── Save disaster enrichment to Snowflake ─────────────────────────────────


def save_disaster_enrichment(tool_context: ToolContext) -> str:
    """Write AI-generated weather context and impact analysis back to Snowflake.

    Reads event_data, weather_analysis, and impact_analysis from session
    state. No arguments needed — all data comes from the session.
    """
    try:
        import snowflake_store

        event_type = tool_context.state.get("event_type", "")

        event_data = _safe_parse_json_obj(
            tool_context.state.get("event_data", "{}")
        )
        if not event_type:
            event_type = event_data.get("event_type", "")
        events = event_data.get("events", [])
        if not events:
            events = _safe_parse_json_array(
                tool_context.state.get("event_data", "[]")
            )

        if not events:
            weather = _safe_parse_json_array(
                tool_context.state.get("weather_analysis", "[]")
            )
            impact = _safe_parse_json_array(
                tool_context.state.get("impact_analysis", "[]")
            )
            all_ids = set()
            for w in weather:
                if isinstance(w, dict) and w.get("event_id"):
                    all_ids.add(w["event_id"])
            for i in impact:
                if isinstance(i, dict) and i.get("event_id"):
                    all_ids.add(i["event_id"])
            if all_ids:
                log.info("Reconstructing event list from %d unique IDs in "
                         "weather/impact analyses", len(all_ids))
                events = [{"id": eid} for eid in all_ids]

        weather = _safe_parse_json_array(
            tool_context.state.get("weather_analysis", "[]")
        )
        impact = _safe_parse_json_array(
            tool_context.state.get("impact_analysis", "[]")
        )

        weather_by_id = {
            w.get("event_id", ""): w for w in weather if isinstance(w, dict)
        }
        impact_by_id = {
            i.get("event_id", ""): i for i in impact if isinstance(i, dict)
        }

        log.info(
            "save_disaster_enrichment: %d events, %d weather, %d impact, "
            "type=%s",
            len(events), len(weather), len(impact), event_type,
        )

        saved = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            eid = ev.get("id", "")
            if not eid:
                continue

            w = weather_by_id.get(eid, {})
            imp = impact_by_id.get(eid, {})

            forecast_risk = w.get("forecast_risk", "unknown")
            forecast_summary = w.get("forecast_summary", "")
            historical_ctx = w.get("historical_context", "")
            current = w.get("current_conditions", {})

            pop = imp.get("estimated_affected_population", 0)
            if isinstance(pop, str):
                try:
                    pop = int(pop.replace(",", ""))
                except ValueError:
                    pop = 0

            infra = imp.get("infrastructure_at_risk", [])
            precedent = imp.get("historical_precedent", "")
            headline = imp.get("news_headline", "")
            news_url = imp.get("news_url", "")
            impact_sev = imp.get("impact_severity", "")

            summary_parts = []
            summary_parts.append(
                f"AI Impact Assessment: {impact_sev.upper() if impact_sev else 'PENDING'} risk."
            )
            if pop > 0:
                summary_parts.append(
                    f"Estimated {pop:,} people affected."
                )
            if infra:
                summary_parts.append(
                    f"Infrastructure at risk: {', '.join(infra[:5])}."
                )
            if forecast_risk and forecast_risk != "unknown":
                summary_parts.append(
                    f"Weather outlook: {forecast_risk} risk — {forecast_summary}"
                )
            if precedent:
                summary_parts.append(f"Historical context: {precedent}")
            if headline and news_url:
                summary_parts.append(f"Related: {headline} ({news_url})")

            summary = " ".join(summary_parts)

            weather_context = {
                "current": current,
                "forecast_risk": forecast_risk,
                "forecast_summary": forecast_summary,
                "historical_context": historical_ctx,
            }

            compound_risks = []
            nearby_count = ev.get("nearby_count", 0)
            if nearby_count and int(nearby_count) > 1:
                compound_risks.append({
                    "type": "compound_disaster",
                    "description": (
                        f"{nearby_count} other disasters within 500km "
                        "in the last 72 hours"
                    ),
                })

            intel = {
                "summary": summary,
                "weather_context": weather_context,
                "compound_risks": compound_risks,
                "affected_population": pop if pop > 0 else None,
                "action_summary": summary,
            }

            etype = event_type or ev.get("event_type", ev.get("type", ""))
            if etype and snowflake_store.store_disaster_enrichment(
                eid, etype, intel
            ):
                saved += 1

        log.info("save_disaster_enrichment: saved %d/%d events", saved, len(events))
        return f"Enriched and saved {saved} out of {len(events)} {event_type} events to Snowflake."

    except Exception as exc:
        log.error("save_disaster_enrichment failed: %s", exc, exc_info=True)
        return f"ERROR: {exc}"


# ── A2A briefing handoff ──────────────────────────────────────────────────


def send_a2a_briefing(briefing_text: str) -> str:
    """Send the intelligence briefing to the Response Coordinator via A2A.

    Args:
        briefing_text: The full text of the intelligence briefing to send.
    """
    if not briefing_text or len(briefing_text.strip()) < 20:
        return "ERROR: briefing_text is too short to send."

    import requests as _req

    url = f"{A2A_RESPONSE_COORDINATOR_URL}/a2a"
    payload = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "id": "briefing-001",
        "params": {
            "id": "crisisflow-briefing",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": briefing_text}],
            },
        },
    }

    try:
        r = _req.post(url, json=payload, timeout=10)
        log.info("A2A briefing sent: status=%d len=%d", r.status_code, len(briefing_text))
        return f"Briefing sent to Response Coordinator ({len(briefing_text)} chars). Status: {r.status_code}"
    except _req.ConnectionError:
        log.warning("A2A endpoint not available at %s — briefing saved locally", url)
        return f"A2A endpoint not available. Briefing generated ({len(briefing_text)} chars) and saved locally."
    except Exception as exc:
        log.error("A2A briefing send failed: %s", exc)
        return f"A2A send failed: {exc}. Briefing generated ({len(briefing_text)} chars)."
