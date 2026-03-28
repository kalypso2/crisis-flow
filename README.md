# CrisisFlow — Autonomous Disaster Response System

A multi-agent system that ingests real-world crisis signals in real time,
classifies them, scores their severity, allocates resources, and streams
decisions to a live 3D globe dashboard.

---

## Data Sources

### Natural Disaster Sources

| Source | What is downloaded | Endpoint | Poll rate |
|---|---|---|---|
| **USGS Earthquakes** | GeoJSON FeatureCollection of recent quakes: magnitude, depth, location, PAGER alert colour (green/yellow/orange/red), time | `https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&minmagnitude=4.0&limit=50` | Every 30 s |
| **NOAA Weather Alerts** | Active NWS alerts: event type (Tornado Warning, Hurricane Warning, Flash Flood, etc.), severity, urgency, affected area polygon, headline | `https://api.weather.gov/alerts/active?status=actual&message_type=alert` | Every 60 s |
| **GDACS** | RSS/JSON feed of global disaster alerts: earthquakes, floods, cyclones, volcanoes, wildfires, droughts — each with a green/orange/red alert score | `https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH` | Every 5 min |
| **NASA EONET** | Open natural events: wildfires, severe storms, icebergs, volcanic activity — geometry as GeoJSON point or polygon | `https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=50` | Every 60 s |

### Conflict & Social Sources

| Source | What is downloaded | Endpoint | Poll rate |
|---|---|---|---|
| **ACLED** | Armed conflict events: battles, explosions, drone strikes, civilian targeting — with fatalities, actor names, country, precise lat/lon | `https://api.acleddata.com/acled/read?key=KEY&email=EMAIL&limit=50&event_date=TODAY` | Every 5 min |
| **Twitter / X** | Filtered stream of tweets matching conflict keywords: "drone strike", "airstrike", "shelling", "bombing", "missile strike", "ground offensive", "airstrike" — English only, geolocated where possible | `https://api.twitter.com/2/tweets/search/stream` (filtered stream v2) | Live stream (reconnects on drop) |

---

## System Architecture

```
[Data Sources] → [Adapters] → [Event Queue] → [Agent Pipeline] → [Dashboard]
```

Each adapter normalises its source into a shared `CrisisEvent` schema before
anything touches the queue. This means you can swap or add sources without
touching the agents.

### CrisisEvent Schema

```python
@dataclass
class CrisisEvent:
    id: str                  # uuid4
    type: str                # earthquake | flood | cyclone | volcano | wildfire |
                             # drought | storm | conflict | iceberg
    severity: int            # 1 (low) – 5 (critical)
    lat: float
    lon: float
    radius_km: float         # estimated impact radius
    timestamp: datetime
    source: str              # usgs | noaa | gdacs | eonet | acled | twitter
    status: str              # active | resolved
    title: str               # human-readable one-liner
    affected_population: int # estimated from pop-density grid
    confidence: float        # 0.0–1.0, set by Detection agent
    raw: dict                # original API payload, preserved for agents
```

---

## Agent Pipeline

Events flow through five agents in sequence. Each agent receives a
`CrisisEvent`, enriches or transforms it, and passes it on. Agents also emit
a **confidence score** and a **reasoning string** — these feed the consensus
engine.

### Agent 1 — Detection Agent

**Purpose:** Decide whether an incoming event is a genuine crisis worth
escalating, or noise to drop.

**What it does:**
- Checks `severity >= 2` and `confidence >= 0.4` as a minimum bar
- Cross-references the event location against a known-quiet-zone list
- For Twitter events: runs a lightweight keyword-density check and discards
  tweets that match keywords but are clearly not reporting events (e.g.
  historical references, opinion pieces)
- Sets `event.confidence` based on source reliability weight:
  - USGS / NOAA / GDACS: 0.9 (authoritative)
  - EONET: 0.8
  - ACLED: 0.85
  - Twitter: 0.4–0.65 (depends on keyword match count and account age)
- Emits: `DetectionResult(event, accepted: bool, confidence: float, reason: str)`

### Agent 2 — Classification Agent

**Purpose:** Assign a fine-grained event type and attach domain metadata.

**What it does:**
- Maps raw source categories to the canonical `type` enum
- For GDACS events: reads `eventtype` field (EQ, FL, TC, VO, WF, DR)
- For NOAA: maps NWS `event` strings ("Tornado Warning" → `storm`, "Flash
  Flood Warning" → `flood`, "Hurricane Warning" → `cyclone`)
- For ACLED: maps `event_type` + `sub_event_type` ("Air/drone strike" →
  `conflict`, sub-type preserved in `raw`)
- For Twitter: classifies based on which keyword cluster matched
- Adds `domain_tags`: e.g. `["seismic", "infrastructure_risk"]` for earthquakes,
  `["civilian_area", "urban"]` for conflict events in cities
- Emits: `ClassificationResult(type, domain_tags, confidence, reason)`

### Agent 3 — Severity Scoring Agent

**Purpose:** Produce a normalised 1–5 severity score using source-specific
signals.

**Scoring rules by type:**

| Event type | Signal | Score mapping |
|---|---|---|
| Earthquake | USGS magnitude | 4.0–4.9 → 2, 5.0–5.9 → 3, 6.0–6.9 → 4, 7.0+ → 5 |
| Earthquake | PAGER alert | green=+0, yellow=+0, orange=+1, red=+2 (capped at 5) |
| Storm / cyclone | NOAA severity | Minor→1, Moderate→2, Severe→3, Extreme→4, + urgency boost |
| Flood | GDACS alert | green→2, orange→3, red→4, + affected pop bonus |
| Conflict | ACLED fatalities | 0→2, 1–10→3, 11–50→4, 50+→5 |
| Twitter conflict | keyword matches | 1 match→2, 3+→3, geo-verified→+1 |
| Wildfire | EONET / GDACS area | radius_km < 10→2, 10–50→3, 50+→4 |

- Also considers `affected_population`: events affecting > 100k people get +1
- Emits: `SeverityResult(score: int, raw_score: float, confidence, reason)`

### Agent 4 — Resource Allocation Agent

**Purpose:** Decide what type of response to dispatch and from where.

**What it does:**
- Selects response type based on `event.type` + severity:
  - Earthquake sev 4–5: search & rescue teams + medical units + satellite comms
  - Flood sev 3+: evacuation coordination + water rescue + emergency shelter
  - Cyclone: pre-positioning of supplies + evacuation routes
  - Conflict sev 4–5: humanitarian corridor flags + NGO alerts
  - Wildfire: aerial suppression recommendation + evacuation perimeter
- Picks nearest available resource depot from a configurable depot list
- Calculates estimated response time (straight-line km / avg speed)
- Emits: `AllocationResult(resources: List[Resource], eta_minutes: int, depot_lat, depot_lon, confidence, reason)`

### Agent 5 — Communication Agent

**Purpose:** Produce a human-readable action summary for the dashboard.

**What it does:**
- Combines all upstream agent results into a single `ActionSummary`
- Formats: `"[SEVERITY 4 EARTHQUAKE] M6.2 near Kathmandu, Nepal. Deploying
  search & rescue from Delhi depot. ETA 4h20m. Est. 280,000 affected."`
- Tags the event for the globe overlay: pulse colour, arc source, arc
  destination
- Streams the summary to the SSE endpoint

---

## Consensus Engine & Fallback Logic

The consensus engine runs **after all five agents complete** and before the
`ActionSummary` is emitted. It handles three scenarios:

### Scenario 1 — Agreement (happy path)

All agents return `confidence >= 0.6` and their outputs are consistent (same
`type`, severity within 1 point of each other). The pipeline proceeds
immediately with no intervention.

### Scenario 2 — Soft disagreement

Triggered when:
- Severity scores differ by more than 1 between agents, OR
- Classification agent and detection agent assign different types, OR
- Any single agent returns `confidence < 0.6`

**Resolution:**
1. Compute a **weighted average severity**: `(detection_conf * sev + classification_conf * sev + scoring_conf * sev) / sum(confidences)`
2. Take the **majority-vote type** across agents
3. Flag the event with `consensus_flag: "LOW_CONFIDENCE"` in the dashboard
4. Reduce the allocation scope by one tier (e.g. sev-5 response → sev-4 response) as a conservative hedge
5. Log the disagreement with each agent's `reason` string for auditing

### Scenario 3 — Hard disagreement or agent failure

Triggered when:
- Two or more agents return `confidence < 0.4`, OR
- An agent raises an exception or times out (> 5s), OR
- Detection agent rejects an event that GDACS/USGS gave a red alert

**Resolution — Fallback logic:**
1. **Source authority override**: If the raw source is USGS red-alert or GDACS
   red-alert, the event is **always escalated to severity 4 minimum**,
   regardless of agent votes. Authoritative government APIs trump agent uncertainty.
2. **Dead agent substitution**: If an agent times out, its last known good
   result for a similar event type is cloned and used with `confidence -= 0.2`.
   If no prior result exists, a safe default is used (severity 3, type from
   source, allocation = first-aid only).
3. **Circuit breaker**: If the same agent fails 3 times in 60 seconds, it is
   marked `DEGRADED` and bypassed for 5 minutes. Its slot in the pipeline is
   filled by the fallback defaults above.
4. The event is flagged `consensus_flag: "FALLBACK_USED"` and highlighted in
   the dashboard with an amber border.

---

## Running the System

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in API keys
cp config/keys.example.env config/keys.env

# 3. Start the pipeline (ingestion + agents + SSE server)
python pipeline/main.py

# 4. Start the dashboard (separate terminal)
cd dashboard && npm install && npm start
```

Dashboard runs at `http://localhost:3000`
SSE feed at `http://localhost:8000/stream`
REST API at `http://localhost:8000/events`
