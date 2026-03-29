# CrisisFlow — Autonomous AI Disaster Response System

CrisisFlow is a multi-agent AI pipeline that ingests live crisis signals from global data sources, processes them through 9 AI agents powered by Google Gemini, allocates humanitarian aid from UN depots, and serves a real-time 3D globe dashboard.

---

## How It Works — High Level

```
Live Data Sources
  USGS · NOAA · GDACS · EONET
          │
          ▼
   Priority Queue  (severity-ordered, deduplicated)
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│                    Agent Pipeline                           │
│                                                             │
│  1. Detection     → Is this a real, actionable crisis?      │
│  2. Classification → What type? What tags?                  │
│  3. Severity       → How bad is it? (1–5)                   │
│  4. [Debate]       → Resolve disagreements in severity      │
│  5. Allocation     → Which UN hubs send what aid?           │
│  6. Consensus      → Are all agents in agreement?           │
│  7. Coordinator    → Final coherent decision review         │
│  8. Communication  → Generate public + operational alerts   │
│  9. Reflection     → Was the response appropriate?          │
│ 10. Simulation     → What if this escalates?               │
└─────────────────────────────────────────────────────────────┘
          │
          ▼
   Snowflake Storage  +  SSE Stream  →  React Dashboard
```

Every agent runs with a **45-second timeout** and degrades gracefully — if Gemini is unavailable or an agent fails, the rule-based result is preserved and the pipeline continues.

---

## Data Sources

| Source | Poll Interval | What It Provides |
|--------|--------------|------------------|
| **USGS** | 120 s | Earthquakes — magnitude, PAGER alert level |
| **NOAA** | 300 s | Active NWS weather alerts — hurricanes, tornadoes, floods |
| **GDACS** | 600 s | Global Disaster Alert — cyclones, volcanoes, droughts |
| **EONET** | 300 s | NASA natural events — wildfires, icebergs, sea/lake ice |

All sources are normalized into a shared `CrisisEvent` schema before entering the pipeline. Events with placeholder coordinates `(0, 0)` or invalid lat/lon are **quarantined** and never processed.

---

## The 9 Agents

### 1. Detection Agent
**File:** [agents.py](agents.py) · **Role:** Gatekeeper

Decides whether an event is worth acting on. First applies two hard rules:
- Severity must be ≥ 2
- Must have valid coordinates (unless from an authoritative source)

Then asks Gemini: *"Is this event actionable — active, urgent, and relevant — or is it stale, duplicate, or irrelevant?"*

Only overrides to rejected if Gemini confidence ≥ 0.90. This prevents high-severity alerts from being dropped on marginal AI calls.

**Output:** `accepted: bool`, `confidence`, `reasoning`

---

### 2. Classification Agent
**File:** [agents.py](agents.py) · **Role:** Categorizer

Assigns a canonical event type and domain tags using source-specific rules:
- USGS → always `earthquake`
- NOAA → maps NWS event strings (e.g. "Hurricane Warning" → `cyclone`)
- GDACS → reads `eventtype` field
- EONET → reads `categories[0].id`

Then asks Gemini to validate the type and suggest additional tags based on location context, compound effects, and secondary hazards. Tags from both rule-based and AI sources are merged (union).

**Event types:** `earthquake · flood · cyclone · volcano · wildfire · drought · storm · conflict · iceberg`

**Output:** `event_type`, `domain_tags`, `confidence`, `reasoning`

---

### 3. Severity Agent
**File:** [agents.py](agents.py) · **Role:** Risk Scorer

Produces a 1–5 severity score using source-specific rules:

| Source | Rule |
|--------|------|
| USGS | Magnitude: 4.x→2, 5.x→3, 6.x→4, 7+→5; PAGER orange +1, red +2 |
| NOAA | Severity string: Minor→1 · Moderate→2 · Severe→3 · Extreme→4; Immediate urgency +1 |
| GDACS | Alert level: green→2 · orange→3 · red→4 |
| All | Affected population > 100,000 → +1 bonus (capped at 5) |

Gemini then validates the rule score considering urban/rural context, population density, and infrastructure vulnerability. AI score is only adopted if confidence ≥ 0.75 and within 1 point of the rule score.

**Output:** `score (1–5)`, `confidence`, `reasoning`

---

### 4. Debate Agent *(conditional)*
**File:** [coordinator.py](coordinator.py) · **Role:** Arbiter

Only fires when the adapter's initial severity estimate and the Severity Agent's score **diverge by 2 or more points**. Presents both positions to Gemini as a structured debate and returns a winning score with justification. Can synthesize both positions if neither is clearly correct.

**Triggers:** `|adapter_severity - agent_severity| ≥ 2`

**Output:** `winning_score`, `winning_position (A/B/synthesis)`, `debate_summary`

---

### 5. Allocation Agent
**File:** [agents.py](agents.py) + [hub_agents.py](hub_agents.py) · **Role:** Aid Dispatcher

Orchestrates resource allocation from 6 real UN humanitarian depots (UNHRD network):

| Hub | Location | Org | Specializations |
|-----|----------|-----|-----------------|
| Brindisi UNHRD | Italy | UNHRD/WFP | Earthquake, Flood, Conflict |
| Dubai UNHRD | UAE | UNHRD/OCHA | Conflict, Drought, Flood |
| Accra UNHRD | Ghana | UNHRD/WFP | Conflict, Drought, Flood |
| Kuala Lumpur UNHRD | Malaysia | UNHRD/OCHA | Cyclone, Flood, Earthquake |
| Panama City UNHRD | Panama | UNHRD/WFP | Cyclone, Flood, Earthquake |
| Las Palmas UNHRD | Spain | UNHRD/WFP | Flood, Drought, Conflict |

**How it works:**
1. Calculates aid need based on event type, severity, and affected population
2. Each `HubAgent` checks its live inventory and bids on what it can contribute
3. Gemini receives all hub proposals and selects the optimal combination
4. A greedy selection fills remaining need from the Gemini-preferred ordering
5. Inventory is committed (deducted from live depot stock)
6. Returns a full convoy manifest with per-hub contributions and ETAs

Transport mode is selected by distance: air (<3,000 km), sea (>3,000 km), or land (same continent).

**Output:** `resources`, `eta_minutes`, `depot_name`, `convoys[]`, `need`, `allocation_reasoning`

---

### 6. Consensus Engine
**File:** [consensus.py](consensus.py) · **Role:** Integrity Checker

Runs after the first 5 agents to detect and resolve disagreements before the Coordinator sees results.

| Outcome | Trigger | Action |
|---------|---------|--------|
| **Agreement** | Severity spread ≤ 1 and types match | `consensus_flag` = empty, proceed |
| **Soft disagreement** | Spread > 1 or classification type ≠ adapter type | Average severity, majority-vote type, drop last resource, flag = `LOW_CONFIDENCE` |
| **Hard failure** | Agent timed out, crashed, or authority override triggered | Substitute from cache or safe defaults, flag = `FALLBACK_USED` |

**Authority override:** USGS PAGER red/orange, NOAA Extreme, or GDACS red forces severity ≥ 4 regardless of agent scores.

**Circuit breaker:** 3 failures within 60 seconds → agent marked DEGRADED for 5 minutes. Degraded agents are skipped entirely. Status visible at `GET /health`.

---

### 7. Coordinator Agent
**File:** [coordinator.py](coordinator.py) · **Role:** Final Decision Reviewer

Reviews all upstream agent results holistically and makes a final coherent call. Checks for internal inconsistencies (e.g. severity 5 earthquake with minimal resources), and can override the severity score if it's clearly wrong.

**Output:** `final_recommendation (proceed/escalate/downgrade)`, `severity_override`, `inconsistency_detected`, `reasoning`

---

### 8. Communication Agent
**File:** [agents.py](agents.py) · **Role:** Alert Generator

Builds a structured action summary and globe rendering parameters, then asks Gemini to generate two distinct communications:

- **`citizen_alert`** — Max 2 sentences, plain language, for the affected public
- **`operational_summary`** — Detailed internal brief for response coordinators: event context, resources deploying, logistics, key concerns

Also produces globe visualization data: event color by type, convoy arc source/destination, and per-hub arc entries for the 3D globe.

**Output:** `summary`, `citizen_alert`, `operational_summary`, `globe_color`, `arcs[]`

---

### 9. Reflection Agent
**File:** [coordinator.py](coordinator.py) · **Role:** Post-Decision Critic

After the full response has been decided, Reflection reviews everything critically and asks: *"Was this response appropriate? Were we under- or over-resourced? What would we do differently?"*

This is a self-assessment layer — it doesn't change anything but surfaces concerns and improvement suggestions for operators reviewing the response.

**Output:** `assessment (appropriate/under-resourced/over-resourced)`, `concerns`, `suggestions`, `reasoning`

---

### 10. Simulation Agent
**File:** [coordinator.py](coordinator.py) · **Role:** Escalation Projector

Projects what happens if the disaster escalates to the next severity level (`current + 1`, capped at 5). Estimates:
- Projected affected population
- Additional resources that would be needed
- Which hubs should pre-position supplies now as a precaution
- An actionable early warning message

**Output:** `projected_severity`, `projected_affected_population`, `additional_resources[]`, `pre_position_hubs[]`, `early_warning`

---

## Pipeline Flow in Detail

```
Event enters queue
       │
       ├─ [Detection] ──── rejected? → drop event (logged)
       │
       ├─ [Classification] ──── event_type + domain_tags
       │
       ├─ [Severity] ──── score 1–5
       │       │
       │       └─ spread ≥ 2? → [Debate] → winning_score overrides
       │
       ├─ [Allocation] ──── hub bids → Gemini selection → convoy manifest
       │
       ├─ [Consensus] ──── agreement / soft / hard resolution
       │
       ├─ [Coordinator] ──── final review, optional severity_override
       │
       ├─ [Communication] ──── citizen_alert + operational_summary + globe data
       │
       ├─ [Reflection] ──── post-decision critique
       │
       └─ [Simulation] ──── escalation projection
              │
              ▼
        event.agent_reasoning = {all 9 agents}
              │
              ▼
       Snowflake storage + SSE broadcast → dashboard
```

---

## AI Integration

**Model:** `gemini-2.5-flash` (Google Gemini)

**File:** [claude_manager.py](claude_manager.py)

All agents use a single `ClaudeManager` wrapper with `.call(prompt)` interface. The wrapper includes:
- **MD5-keyed disk cache** at `data/cached_responses/` — identical prompts are never re-billed
- Graceful error handling — any Gemini failure returns the rule-based result unchanged

**Cost estimate:** ~$0.0007 per event processed (~$0.04–$1.00/day at normal volumes)

**Required:** `GOOGLE_API_KEY` in `.env`

---

## API Endpoints (port 8000)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/events` | All processed events from Snowflake (with AI reasoning fields) |
| `GET` | `/stream` | SSE stream — new events pushed in real time |
| `GET` | `/health` | Agent status, circuit breaker state, queue depth |
| `GET` | `/inventory` | Live UNHRD hub stock levels vs baselines |
| `POST` | `/distribute` | Run a week-long aid simulation on a set of events |
| `GET` | `/convoys` | Recent convoy dispatch records |
| `GET` | `/quarantine` | Events rejected at ingestion |
| `GET` | `/snowflake/summary` | Row counts per event table |
| `GET` | `/snowflake/<type>` | e.g. `/snowflake/earthquake` |

**Event response fields include:**
- Standard: `id`, `source`, `type`, `severity`, `lat`, `lon`, `title`, `timestamp`, `affected_population`
- AI-generated: `citizen_alert`, `operational_summary`, `action_summary`
- Agent reasoning: `agent_reasoning` — object with keys for each agent that ran
- Globe: `globe_color`, `arc_source`, `arc_dest`, `arcs[]`
- Allocation: `allocation.resources[]`, `allocation.eta_minutes`, `allocation.convoys[]`

---

## Setup

### Backend

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — add GOOGLE_API_KEY, Snowflake credentials

# 3. Run
python main.py
# → http://localhost:8000
```

### Frontend

```bash
npm install
npm run dev
# → http://localhost:5173
```

Both must be running simultaneously. The React app calls the Flask API on port 8000.

**Pipeline mode:** By default, `main.py` does **not** start background ingestion or the agent consumer (so depot stock does not drain on its own). Use the dashboard buttons **Poll sources** / **Process next event**, or `POST /pipeline/poll-sources` and `POST /pipeline/process-next`. To restore the old continuous behaviour, set `CRISISFLOW_AUTO_PIPELINE=1` in `.env`.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_API_KEY` | Yes | Gemini API key — get one at aistudio.google.com |
| `SNOWFLAKE_ACCOUNT` | Yes | Snowflake account identifier |
| `SNOWFLAKE_USER` | Yes | Snowflake username |
| `SNOWFLAKE_PASSWORD` | Yes | Snowflake password |
| `SNOWFLAKE_DATABASE` | No | Default: `CRISISFLOW` |
| `SNOWFLAKE_SCHEMA` | No | Default: `PUBLIC` |
| `SNOWFLAKE_WAREHOUSE` | No | Default: `COMPUTE_WH` |
| `CRISISFLOW_AUTO_PIPELINE` | No | Set to `1` to enable continuous ingest + agent consumer; default is manual pipeline only |

---

## Repository Layout

| File | Role |
|------|------|
| [main.py](main.py) | Flask API, event queue, consumer thread, ingestion threads |
| [agents.py](agents.py) | Detection, Classification, Severity, Allocation, Communication agents |
| [coordinator.py](coordinator.py) | Coordinator, Debate, Reflection, Simulation agents |
| [hub_agents.py](hub_agents.py) | Per-hub bidding + MasterAllocationAgent |
| [consensus.py](consensus.py) | Disagreement resolution, circuit breaker, authority override |
| [schema.py](schema.py) | CrisisEvent + all agent result dataclasses |
| [claude_manager.py](claude_manager.py) | Gemini API wrapper with disk caching |
| [snowflake_store.py](snowflake_store.py) | Snowflake read/write, one table per event type |
| [depot_inventory.py](depot_inventory.py) | Live UNHRD hub inventory, commit/replenishment |
| [need_calculator.py](need_calculator.py) | Aid quantity calculation by event type + population |
| [distribution_engine.py](distribution_engine.py) | Week-simulation engine for `POST /distribute` |
| [adapters.py](adapters.py) | USGS, NOAA, GDACS, EONET — fetch + normalize |
| [App.jsx](src/App.jsx) | React dashboard — 3D globe, filters, panels, timeline |
