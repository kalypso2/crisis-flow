# CrisisFlow — Autonomous Disaster Response System

A multi-agent pipeline that ingests **live** crisis signals (earthquakes, weather, GDACS, conflict, social), normalizes them into a shared schema, runs **detection → classification → severity → allocation → communication**, applies a **consensus** layer with circuit breakers, persists to **Snowflake**, deducts **UNHRD-style depot stock**, and serves a **React + Vite** dashboard with a **3D globe**, **week timeline**, and **aid distribution simulation**.

---

## Repository layout

| Path | Role |
|------|------|
| `main.py` | Flask API, priority event queue, consumer thread, ingestion threads, `process_event` |
| `adapters.py` | USGS, NOAA, GDACS, EONET, ACLED, Twitter — fetch/stream → `CrisisEvent` |
| `agents.py` | Five agents: Detection, Classification, Severity, Allocation, Communication |
| `hub_agents.py` | Per-hub bidding + `MasterAllocationAgent` (multi-depot greedy allocation) |
| `consensus.py` | Disagreement resolution, authority override, substitutions, circuit breaker |
| `schema.py` | `CrisisEvent`, agent result datatypes |
| `snowflake_store.py` | One table per event type + `CONVOYS`; read/write for `/events` |
| `depot_inventory.py` | In-memory UNHRD hub baselines, `commit()` deductions, replenishment |
| `need_calculator.py` | Sphere-oriented need quantities; `scrub_aid_need_notes` for clean API payloads |
| `distribution_engine.py` | **Pure** weekly simulation via `POST /distribute` (does not mutate live inventory) |
| `App.jsx` | Dashboard: globe, filters, aid/detail/hub panels, distribution feed, timeline |
| `gemini_manager.py` / `gemini_strategies.py` | Gemini key rotation and caching |
| `timestamp_agent.py` | Optional: refine ACLED static JSON timestamps |
| `data/` | ACLED static GeoJSON / markers (optional ingest) |

---

## Data sources & ingestion

Poll intervals are defined in `main.py` → `start_ingestion_threads()`. **Twitter** uses a **filtered stream** (not a poll).

| Source | Interval | Notes |
|--------|----------|--------|
| USGS | 120 s | Earthquakes GeoJSON |
| NOAA | 300 s | Active NWS alerts |
| GDACS | 600 s | Global disaster list |
| EONET | 300 s | NASA natural events |
| ACLED | 900 s | Requires `ACLED_API_KEY` + `ACLED_EMAIL` |


---

## Architecture

```
[Adapters] → _enqueue (dedup TTL + coord quarantine) → PriorityQueue (−severity, counter)
       → consumer_thread → process_event → Snowflake + convoys
       → GET /events, /inventory, POST /distribute, …

[Browser]  →  Vite (e.g. :5173)  →  App.jsx  →  API :8000
```

- **Queue ordering:** Higher `severity` first; monotonic counter breaks ties.
- **Quarantine:** Non-finite lat/lon, out-of-range, or `(0,0)` placeholders are rejected (logged).
- **Dedup:** Same `event.id` within `DEDUP_TTL` (~10 min) is dropped.

---

## `CrisisEvent` (`schema.py`)

Fields include: `id`, `source`, `type`, `domain_tags`, `lat`, `lon`, `radius_km`, `location_name`, `severity`, `affected_population`, `timestamp`, `status`, `title`, `action_summary`, `consensus_flag`, `allocation`, `raw`.

Processed API payloads may add: `globe_color`, `arc_source`, `arc_dest`, `arcs` (multi-hub legs for legacy pipeline arcs; the UI globe **simulation arcs** come from `POST /distribute` feed in `App.jsx`).

---

## Agent pipeline (`process_event` in `main.py`)

Each stage uses `_run_with_timeout` (**`AGENT_TIMEOUT`**, default **5 s**).

1. **Detection** — Requires `severity >= 2`; valid location unless source is authoritative with special rules; Twitter **historical-reference** filter. `DetectionResult(accepted, reason)`.
2. **Classification** — Maps source categories to canonical `type` + `domain_tags`. `ClassificationResult`.
3. **Severity** — Source-specific rules on `raw` (USGS mag/PAGER, NOAA, GDACS, ACLED fatalities, Twitter keywords/geo); optional `affected_population` bonus. `SeverityResult`.
4. **Allocation** — `MasterAllocationAgent` solicits bids from UNHRD hubs, fills `need_calculator.calculate` output, **`depot_inventory.commit`**, returns convoys. `AllocationResult`.
5. **Communication** — Human-readable summary + globe hints. `CommunicationResult`.

If detection rejects the event, `process_event` returns `None` and nothing is broadcast.

---

## Consensus (`consensus.py`)

Runs after all agents; `None` results become cache hits or safe defaults.

| Outcome | When | Effect |
|---------|------|--------|
| Agreement | `|adapter_sev − agent_sev| ≤ 1` and types align | `consensus_flag` empty |
| Soft | Spread > 1 **or** classification type ≠ adapter type | `LOW_CONFIDENCE`; average severity; majority type; **drop last** planned resource |
| Hard | Failed agents / substitution / **authority override** | `FALLBACK_USED`; same averaging + reduction |

**Authority override:** USGS PAGER orange/red, NOAA Extreme, or GDACS red can force severity **≥ 4**.

**Circuit breaker:** Three failures in 60 s → agent **DEGRADED** for 300 s; `main.py` skips degraded agents. **`GET /health`** exposes per-agent status.

---

## Live inventory vs week simulation

| Mechanism | Mutates `depot_inventory` | Purpose |
|-----------|---------------------------|---------|
| Pipeline allocation | Yes | Real-time commits when events process |
| `simulate_week` (`distribution_engine.py`) | **No** | Resets from baselines; powers UI bars, feed, staggered globe arcs |

---

## HTTP API (Flask, default **8000**)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Queue depth, processed count, agent OK/DEGRADED |
| GET | `/events` | Snowflake union by `TYPE_TO_TABLE`, deduped by `(title, date)`; fallback in-memory buffer; notes scrubbed |
| GET | `/stream` | SSE: heartbeats + new processed events as JSON |
| GET | `/inventory` | Hub stock, baselines, `stock_level` |
| POST | `/distribute` | Body: JSON array of events → `{ committed, need, feed, events_served, events_unmet }` |
| GET | `/quarantine` | Quarantined ingest rows |
| GET | `/acled-static` | Conflicts from Snowflake or `data/*.json` fallback |
| GET | `/convoys` | Recent convoy rows |
| GET | `/snowflake/summary` | Row counts per table |
| GET | `/snowflake/<event_type>` | e.g. `earthquake`, `conflict` (special params) |

---

## Snowflake (`.env`)

Typical variables (`snowflake_store.py`): `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, optional `SNOWFLAKE_DATABASE` (default `CRISISFLOW`), `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_WAREHOUSE`.

On startup, `main.py` calls `drop_confidence_column()` and `ensure_convoys_table()`. If Snowflake is unavailable, `/events` falls back to the in-memory ring buffer.

---

## Frontend

```bash
npm install
npm run dev
```

Default: **http://127.0.0.1:5173**. API base: same host, port **8000**, unless **`VITE_API_ORIGIN`** is set (`App.jsx` → `apiOrigin()`).

**Stack:** React 19, Vite 5, `react-globe.gl`, Three.js.

**UI highlights:** Event list + type filter, stats bar, **week scrubber** (re-runs distribution), **Aid** / **Details** / **hub inventory** panels, **distribution feed** (rows open Aid for that `event_id`), hex heatmap, **simulation arcs** (staggered, filtered by event type).

---

## Backend setup

```bash
pip install -r requirements.txt
```

Project root **`.env`** is loaded by `dotenv` in `main.py`.

```bash
python3 main.py
```

Binds **`0.0.0.0:8000`**. The module docstring may mention `pipeline/main.py`; in this repo the entry point is **`main.py`** at the root.

**Python deps (excerpt):** Flask, flask-cors, requests, snowflake-connector, pandas, `google-genai` / `google-generativeai`, Pillow.

---

## Optional: static ACLED JSON

`ingest_static_acled_once()` in `main.py` reads `data/acled_globe_markers_refined.json` (preferred) or `data/acled_globe_markers.json`. It is **not** invoked automatically in the `if __name__ == "__main__"` block—call it from a one-off script or add a startup hook if you want those rows in the queue. **`python3 timestamp_agent.py`** can build the refined file.

---

## Package metadata

`package.json` has `"name": "files"` (placeholder). The product name is **CrisisFlow**.
