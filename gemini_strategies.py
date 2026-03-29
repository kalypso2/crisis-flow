"""
CrisisFlow — Fat prompt strategy (examples)
Since rate limits are RPD (requests per day) not TPD (tokens per day),
pack maximum work into each call. One call can process many conflict
zones instead of one call per zone.

Free tier: large token limits per minute; each request still counts toward RPD.
"""

from __future__ import annotations

import json

# ═══════════════════════════════════════════════════════════════
# EXAMPLE 1: BATCH ALL CONFLICT ZONES IN ONE CALL
# ═══════════════════════════════════════════════════════════════

BATCH_CONFLICT_PROMPT = """You are a conflict intelligence analyst for CrisisFlow,
a global disaster response system.

Below is ACLED conflict data for the {zone_count} most severe active conflict
zones on Earth. For EACH zone, provide an intelligence assessment.

DATA:
{all_zones_json}

For EACH zone, output:
1. status: "escalating" | "stable" | "de-escalating"
2. summary: 2-sentence human-readable description of what's happening
3. civilian_risk: "critical" | "high" | "moderate" | "low"
4. estimated_displaced: number (your best estimate from context)
5. key_actors: list of primary parties involved
6. recommended_alert_level: "red" | "orange" | "yellow"

Output a single JSON array with one object per zone:
[
  {{
    "zone_id": "ACLED-SYR-Aleppo",
    "country": "Syria",
    "region": "Aleppo",
    "status": "escalating",
    "summary": "...",
    "civilian_risk": "critical",
    "estimated_displaced": 2100000,
    "key_actors": ["Syrian govt forces", "Turkish-backed SNA", "SDF"],
    "recommended_alert_level": "red"
  }},
  ...
]

Analyze ALL {zone_count} zones. Do not skip any."""


def build_batch_conflict_prompt(zones: list[dict]) -> str:
    """Build one mega-prompt that analyzes all conflict zones at once."""
    return BATCH_CONFLICT_PROMPT.format(
        zone_count=len(zones),
        all_zones_json=json.dumps(zones, indent=2),
    )


# ═══════════════════════════════════════════════════════════════
# EXAMPLE 2: CONVERGENCE + VULNERABILITY + OUTREACH IN ONE CALL
# ═══════════════════════════════════════════════════════════════

CONVERGENCE_MEGA_PROMPT = """You are CrisisFlow's convergence and response engine.
Complete ALL THREE steps in a single analysis.

══════ STEP 1: CONVERGENCE CHECK ══════
GLOBAL THREAT DATA:
{global_threat}

LOCAL SENSOR DATA:
{local_sensor}

Questions to answer:
- Do the global and local signals describe the same event?
- Is the local sensor consistent with the global forecast?
- Confidence score: 0.0 to 1.0
- Recommendation: DEPLOY (confidence >= 0.7) | VERIFY | MONITOR

══════ STEP 2: VULNERABILITY ANALYSIS ══════
(Only complete if Step 1 confidence >= 0.7)

CDC SOCIAL VULNERABILITY INDEX DATA:
{svi_data}

NEAREST SHELTERS:
{shelter_data}

Identify:
- Total population at risk
- % elderly (65+), disabled, no vehicle, limited English
- Which shelters can serve which needs (wheelchair, pets)

══════ STEP 3: OUTREACH PLAN ══════
(Only complete if Step 1 confidence >= 0.7)

Create tiered outreach:
- TIER 1 (immediate call): elderly + disabled + no vehicle
  → Count, languages, shelter assignment, ElevenLabs voice script
- TIER 2 (call within 30 min): elderly OR disabled with vehicle
  → Count, languages, shelter assignment, voice script
- TIER 3 (SMS blast): general population in flood zone
  → Count, message text

══════ OUTPUT FORMAT ══════
Return a single JSON object:
{{
  "convergence": {{
    "converged": bool,
    "confidence": float,
    "recommendation": "DEPLOY|VERIFY|MONITOR",
    "reasoning": "1 sentence"
  }},
  "vulnerability": {{
    "total_population": int,
    "pct_elderly": float,
    "pct_disabled": float,
    "pct_no_vehicle": float,
    "pct_limited_english": float,
    "primary_languages": ["en", "es"]
  }},
  "outreach_plan": {{
    "tier_1": {{
      "count": int,
      "languages": ["en", "es"],
      "needs_transport": true,
      "shelter": "shelter name",
      "voice_script_en": "full ElevenLabs script",
      "voice_script_es": "full ElevenLabs script in Spanish"
    }},
    "tier_2": {{...}},
    "tier_3": {{
      "count": int,
      "sms_text": "full SMS message"
    }}
  }}
}}"""


VALIDATION_PROMPT = """Review this CrisisFlow response plan.
Answer ONLY these 3 yes/no questions:

PLAN: {response_plan}

1. Is the confidence score justified by the evidence? (yes/no + 1 sentence)
2. Are shelter assignments within 10km of affected area? (yes/no)
3. Do outreach tier counts add up to total population? (yes/no)

If all yes: output exactly "VALIDATED"
If any no: output which check failed and why (1 sentence each)"""


# ═══════════════════════════════════════════════════════════════
# EXAMPLE 4: WEBCAM — optional (requires opencv-python, numpy)
# ═══════════════════════════════════════════════════════════════

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


class SmartFrameCapture:
    """Only sends frames to Gemini when meaningful change is detected."""

    def __init__(self, threshold: float = 0.05):
        if cv2 is None or np is None:
            raise ImportError(
                "SmartFrameCapture requires opencv-python and numpy: pip install opencv-python numpy"
            )
        self.last_frame = None
        self.threshold = threshold
        self.calls_saved = 0

    def should_analyze(self, frame) -> bool:
        if self.last_frame is None:
            self.last_frame = frame
            return True

        diff = cv2.absdiff(frame, self.last_frame)
        change_ratio = float(np.mean(diff) / 255.0)

        if change_ratio > self.threshold:
            self.last_frame = frame
            return True
        self.calls_saved += 1
        return False


# ═══════════════════════════════════════════════════════════════
# BUDGET TRACKER
# ═══════════════════════════════════════════════════════════════


class BudgetTracker:
    """Track API calls across development / demo."""

    def __init__(self, daily_limit_per_project: int = 250, num_projects: int = 4):
        self.total_budget = daily_limit_per_project * num_projects
        self.calls_made = 0
        self.calls_by_phase = {
            "development": 0,
            "enrichment": 0,
            "demo_prep": 0,
            "live_demo": 0,
        }
        self.current_phase = "development"

    def track(self, model: str = "flash"):
        self.calls_made += 1
        self.calls_by_phase[self.current_phase] += 1

    def set_phase(self, phase: str):
        self.current_phase = phase

    def status(self) -> str:
        remaining = self.total_budget - self.calls_made
        return (
            f"API Budget: {self.calls_made}/{self.total_budget} used, "
            f"{remaining} remaining\n"
            f"By phase: {json.dumps(self.calls_by_phase)}"
        )

    def warn_if_low(self):
        remaining = self.total_budget - self.calls_made
        if remaining < 50:
            print(f"LOW API BUDGET: only {remaining} calls remaining!")
            print("   Switch to demo mode / cached responses if available")
        elif remaining < 100:
            print(f"API budget getting tight: {remaining} calls remaining")
