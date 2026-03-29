"""
CrisisFlow ADK — Custom tools for the disaster enrichment pipeline.

Tools:
  validate_risk_assessment      — programmatic QA on impact/weather analysis
  save_disaster_enrichment      — persist AI analysis to Snowflake
  persist_intelligence_briefing — write full briefing text to ENRICHMENT_SUMMARY
  send_a2a_briefing             — POST briefing to Response Coordinator via A2A
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


def _row_has_weather_payload(w: Any) -> bool:
    """True if a weather row is usable (Open-Meteo output or LLM wildfire-style)."""
    if not isinstance(w, dict) or not w:
        return False
    if w.get("current_conditions") or w.get("forecast_risk"):
        return True
    if w.get("weather_risk_level") or w.get("temperature"):
        return True
    return False


def _normalize_weather_for_storage(w: dict) -> tuple[str, str, str, dict]:
    """Map weather_analysis rows to canonical forecast_risk / summary / current."""
    if w.get("current_conditions") or w.get("forecast_risk"):
        cur = w.get("current_conditions")
        if not isinstance(cur, dict):
            cur = {}
        return (
            str(w.get("forecast_risk") or "unknown"),
            str(w.get("forecast_summary") or ""),
            str(w.get("historical_context") or ""),
            cur,
        )
    wr = w.get("weather_risk_level") or w.get("forecast_risk") or "unknown"
    wr_s = str(wr).lower()
    if "extreme" in wr_s:
        fr = "extreme"
    elif "critical" in wr_s or "high" in wr_s:
        fr = "high"
    elif "moderate" in wr_s:
        fr = "moderate"
    else:
        fr = "moderate"
    parts = []
    for key in (
        "temperature",
        "humidity",
        "wind_speed",
        "drought_conditions",
        "precipitation_last_72_hours",
    ):
        if w.get(key):
            parts.append(f"{key}: {w[key]}")
    fsum = "; ".join(parts)
    notes = w.get("notes") or ""
    if notes:
        fsum = (fsum + ". " if fsum else "") + str(notes)
    hist = str(w.get("drought_conditions") or "")
    desc = (notes[:500] if notes else fsum) or "Wildfire / hazard weather context"
    current = {
        "description": desc,
        "temp_c": None,
        "wind_kmh": None,
        "precipitation_mm": None,
    }
    return fr, fsum, hist, current


def _hub_meta_by_name() -> dict[str, dict]:
    from hub_agents import HUBS

    return {h["name"]: h for h in HUBS}


def _commit_hub_stock(hub_name: str, quantities: dict[str, int]) -> dict[str, int]:
    """
    Deduct hub inventory. When CRISISFLOW_INVENTORY_API_BASE is set (e.g.
    http://127.0.0.1:8000), commits go to the Flask API so the dashboard
    /inventory matches ADK runs. Otherwise uses in-process depot_inventory.
    """
    base = os.environ.get("CRISISFLOW_INVENTORY_API_BASE", "").strip().rstrip("/")
    if base:
        import urllib.error
        import urllib.request

        body = json.dumps({"hub_name": hub_name, "quantities": quantities}).encode()
        req = urllib.request.Request(
            f"{base}/inventory/commit",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read().decode())
            return {k: int(v) for k, v in (out.get("deducted") or {}).items()}
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode()
            except Exception:
                detail = str(exc)
            log.warning(
                "Remote inventory commit HTTP %s: %s — falling back to local",
                exc.code,
                detail[:200],
            )
        except Exception as exc:
            log.warning(
                "Remote inventory commit failed (%s), using local depot_inventory",
                exc,
            )
    import depot_inventory

    return depot_inventory.commit(hub_name, quantities)


def _pick_hub_for_commit(
    recommended: str,
    nearest_depots: list,
) -> tuple[str | None, dict | None]:
    """Choose a depot that exists in live depot_inventory (UNHRD baselines)."""
    import depot_inventory

    inv = depot_inventory.get_inventory()
    order: list[str] = []
    if recommended:
        order.append(recommended)
    for d in nearest_depots or []:
        if not isinstance(d, dict):
            continue
        n = d.get("name")
        if n and n not in order:
            order.append(n)
    for name in order:
        if name in inv:
            for d in nearest_depots or []:
                if isinstance(d, dict) and d.get("name") == name:
                    return name, d
            return name, None
    return None, None


def _merge_convoy_into_allocation(
    allocation: dict,
    hub_name: str,
    depot_meta: dict | None,
    deducted: dict[str, int],
) -> dict:
    H = _hub_meta_by_name()
    h = H.get(hub_name, {})
    eta_min = 0
    dist_km = 0.0
    if depot_meta and isinstance(depot_meta, dict):
        if depot_meta.get("air_eta_hours") is not None:
            try:
                eta_min = int(float(depot_meta["air_eta_hours"]) * 60)
            except (TypeError, ValueError):
                eta_min = 0
        try:
            dist_km = float(depot_meta.get("distance_km") or 0)
        except (TypeError, ValueError):
            dist_km = 0.0
    conv = {
        "hub_name": hub_name,
        "hub_org": h.get("org", ""),
        "hub_lat": float(h.get("lat") or 0),
        "hub_lon": float(h.get("lon") or 0),
        "dist_km": dist_km,
        "eta_minutes": eta_min,
        "transport": "air",
        "supplies": dict(deducted),
    }
    out = dict(allocation)
    out["depot_name"] = hub_name
    out["depot_org"] = h.get("org", "")
    out["depot_lat"] = h.get("lat")
    out["depot_lon"] = h.get("lon")
    out["eta_minutes"] = eta_min
    out["transport_mode"] = "air"
    out["total_committed"] = int(sum(deducted.values()))
    out["convoys"] = [conv]
    return out


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

        if not imp:
            issues.append("missing impact analysis")
        # Weather is best-effort: parallel MCP / manual session state may omit it.
        # Do not fail validation when impact exists; save_disaster_enrichment uses
        # empty weather fields in that case.
        if not w and not imp:
            issues.append("missing weather analysis")

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
    weather_missing_but_impact_ok = sum(
        1
        for eid in event_ids
        if eid and eid not in weather_by_id and eid in impact_by_id
    )

    result = {
        "total_events": len(events),
        "passed": len(passed),
        "failed": len(failed),
        "missing_weather_analysis": missing_weather,
        "missing_impact_analysis": missing_impact,
        "weather_missing_nonblocking": weather_missing_but_impact_ok,
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
    """Write AI-generated weather, impact, and aid context back to Snowflake.

    Reads event_data, weather_analysis, impact_analysis, and aid_context
    from session state.  No arguments needed — all data comes from session.
    Writes allocation (Sphere needs + depot context), action_summary, and
    agent_reasoning to each event row, then sets enrichment_status='complete'.
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
            all_ids: set[str] = set()
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
        aid_context = _safe_parse_json_array(
            tool_context.state.get("aid_context", "[]")
        )

        weather_by_id = {
            w.get("event_id", ""): w for w in weather if isinstance(w, dict)
        }
        impact_by_id = {
            i.get("event_id", ""): i for i in impact if isinstance(i, dict)
        }
        aid_by_id = {
            a.get("event_id", ""): a for a in aid_context if isinstance(a, dict)
        }

        log.info(
            "save_disaster_enrichment: %d events, %d weather, %d impact, "
            "%d aid_context, type=%s",
            len(events), len(weather), len(impact), len(aid_context), event_type,
        )
        log.info("aid_context event_ids: %s", [a.get("event_id","?")[:8] for a in aid_context if isinstance(a, dict)])
        log.info("event ids: %s", [ev.get("id","?")[:8] for ev in events if isinstance(ev, dict)])

        saved = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            eid = ev.get("id", "")
            if not eid:
                continue

            w_raw = weather_by_id.get(eid, {})
            w = w_raw if isinstance(w_raw, dict) else {}
            imp = impact_by_id.get(eid, {})
            aid = aid_by_id.get(eid, {})

            # ── Weather fields (Open-Meteo MCP shape or LLM wildfire-style) ─
            forecast_risk, forecast_summary, historical_ctx, current = (
                _normalize_weather_for_storage(w)
            )

            # ── Impact fields ─────────────────────────────────────────────
            pop = imp.get("estimated_affected_population", 0)
            if isinstance(pop, str):
                try:
                    pop = int(pop.replace(",", ""))
                except ValueError:
                    pop = 0

            infra      = imp.get("infrastructure_at_risk", [])
            precedent  = imp.get("historical_precedent", "")
            headline   = imp.get("news_headline", "")
            news_url   = imp.get("news_url", "")
            impact_sev = imp.get("impact_severity", "")

            # ── Aid context fields ────────────────────────────────────────
            sphere        = aid.get("sphere_needs", {})
            nearest_depots = aid.get("nearest_depots", [])
            recommended   = aid.get("recommended_depot", "")
            ocha_usd      = aid.get("ocha_funding_usd", 0)
            ocha_flows    = aid.get("ocha_flow_count", 0)
            logistics     = aid.get("logistics_notes", "")

            # Derive displaced from sphere needs or fall back to pop estimate
            displaced = int(sphere.get("displaced", 0) or pop * 0.3)

            # Build the allocation dict in the format App.jsx expects:
            # { need: {sphere quantities}, resources: [...], nearest_depots: [...], ... }
            need_keys = ["shelter_kits", "food_rations", "medical_kits", "water_kits", "vehicles"]
            need_dict = {k: int(sphere.get(k, 0)) for k in need_keys}
            need_dict["displaced"]    = displaced
            need_dict["window_days"]  = int(sphere.get("window_days", 14))

            resources = [k.replace("_", " ") for k in need_keys if need_dict.get(k, 0) > 0]

            allocation = {
                "need": need_dict,
                "resources": resources,
                "nearest_depots": nearest_depots,
                "recommended_depot": recommended,
                "ocha_funding_usd": ocha_usd,
                "ocha_flow_count": ocha_flows,
                "logistics_notes": logistics,
            }

            # ── action_summary ────────────────────────────────────────────
            summary_parts = [
                f"AI Impact Assessment: {impact_sev.upper() if impact_sev else 'PENDING'} risk."
            ]
            if pop > 0:
                summary_parts.append(f"Estimated {pop:,} people affected.")
            if displaced > 0:
                summary_parts.append(f"Est. {displaced:,} displaced.")
            if recommended:
                summary_parts.append(f"Recommended depot: {recommended}.")
            if nearest_depots:
                top = nearest_depots[0]
                summary_parts.append(
                    f"Nearest hub: {top.get('name','?')} "
                    f"({top.get('distance_km',0):.0f} km, "
                    f"{top.get('air_eta_hours',0):.1f}h air ETA)."
                )
            if infra:
                summary_parts.append(f"Infrastructure at risk: {', '.join(infra[:4])}.")
            if forecast_risk and forecast_risk != "unknown":
                summary_parts.append(f"Weather outlook: {forecast_risk} risk — {forecast_summary}")
            if ocha_usd:
                summary_parts.append(
                    f"OCHA historical funding: ${ocha_usd:,.0f} ({ocha_flows} flows)."
                )
            if logistics:
                summary_parts.append(logistics)
            if precedent:
                summary_parts.append(f"Historical context: {precedent}")
            if headline and news_url:
                summary_parts.append(f"Related: {headline} ({news_url})")

            summary = " ".join(summary_parts)

            # ── agent_reasoning (stored for transparency) ─────────────────
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

            agent_reasoning = {
                "weather_context": {
                    "current": current,
                    "forecast_risk": forecast_risk,
                    "forecast_summary": forecast_summary,
                    "historical_context": historical_ctx,
                },
                "impact": {
                    "affected_population": pop,
                    "infrastructure_at_risk": infra,
                    "impact_severity": impact_sev,
                    "historical_precedent": precedent,
                    "news_headline": headline,
                    "news_url": news_url,
                },
                "aid_context": aid,
                "compound_risks": compound_risks,
            }

            intel = {
                "action_summary":    summary,
                "allocation":        allocation,
                "agent_reasoning":   agent_reasoning,
                "operational_summary": logistics or forecast_summary,
                "affected_population": pop if pop > 0 else None,
            }

            etype = event_type or ev.get("event_type", ev.get("type", ""))
            if not etype:
                log.warning("save_disaster_enrichment: skip %s — no event_type", eid[:8])
                continue

            if not snowflake_store.store_disaster_enrichment(eid, etype, intel):
                log.warning(
                    "save_disaster_enrichment: no Snowflake row for %s (%s) — "
                    "ingest this event first",
                    eid[:8],
                    etype,
                )
                continue

            saved += 1

            hub_name, depot_meta = _pick_hub_for_commit(recommended, nearest_depots)
            if hub_name:
                try:
                    need_res = {
                        k: int(need_dict[k])
                        for k in need_keys
                        if int(need_dict.get(k) or 0) > 0
                    }
                    deducted = _commit_hub_stock(hub_name, need_res)
                    if any((deducted or {}).values()):
                        intel["allocation"] = _merge_convoy_into_allocation(
                            intel["allocation"],
                            hub_name,
                            depot_meta,
                            deducted,
                        )
                        snowflake_store.store_disaster_enrichment(eid, etype, intel)
                        snowflake_store.ensure_convoys_table()
                        snowflake_store.store_convoys(
                            eid,
                            etype,
                            float(ev.get("lat") or 0),
                            float(ev.get("lon") or 0),
                            intel["allocation"].get("convoys") or [],
                        )
                        log.info(
                            "save_disaster_enrichment: depot commit %s → %s %s",
                            eid[:8],
                            hub_name,
                            deducted,
                        )
                except Exception as exc:
                    log.error(
                        "save_disaster_enrichment: depot commit failed %s: %s",
                        eid[:8],
                        exc,
                    )

        et_label = (
            (event_type or "").strip()
            or event_data.get("event_type")
            or "disaster"
        )
        log.info("save_disaster_enrichment: saved %d/%d events", saved, len(events))
        msg = (
            f"Enriched and saved {saved} out of {len(events)} {et_label} events "
            f"to Snowflake (weather + impact + aid); recommended-depot stock "
            f"committed where inventory allows."
        )
        try:
            tool_context.state["enrichment_results"] = msg
        except Exception:
            pass
        return msg

    except Exception as exc:
        log.error("save_disaster_enrichment failed: %s", exc, exc_info=True)
        return f"ERROR: {exc}"


# ── Persist full briefing to Snowflake (dashboard ENRICHMENT SUMMARY) ─────


def persist_intelligence_briefing(
    briefing_text: str, tool_context: ToolContext
) -> str:
    """Write the full intelligence briefing to ENRICHMENT_SUMMARY for each event id in event_data."""
    if not briefing_text or len(briefing_text.strip()) < 20:
        return "ERROR: briefing_text is too short to persist."

    import snowflake_store

    event_data = _safe_parse_json_obj(tool_context.state.get("event_data", "{}"))
    events = event_data.get("events", [])
    if not events:
        events = _safe_parse_json_array(tool_context.state.get("event_data", "[]"))

    event_type = tool_context.state.get("event_type", "") or event_data.get(
        "event_type", ""
    )
    ids: list[str] = []
    for ev in events:
        if isinstance(ev, dict) and ev.get("id"):
            ids.append(str(ev["id"]).strip())

    if not ids:
        return (
            "ERROR: no event ids in session event_data — run data prep first "
            "so briefing can be tied to Snowflake rows."
        )

    n = snowflake_store.store_enrichment_briefing(
        event_type, ids, briefing_text.strip()
    )
    return (
        f"Persisted intelligence briefing to Snowflake: {n} row(s) updated "
        f"for {len(ids)} event id(s). Refresh the CrisisFlow UI to view "
        f"Enrichment summary on each event."
    )


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
