"""
CrisisFlow — Gemini API Manager
Wraps the Google Generative AI SDK with response caching to avoid redundant API calls.

Setup:
    In .env (repo root), set:
    GOOGLE_API_KEY=AIzaSy...
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import google.generativeai as genai

logger = logging.getLogger("crisisflow.gemini")

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "cached_responses"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MODEL = "gemini-2.5-flash"


class ClaudeManager:
    """Gemini client with MD5-keyed disk caching. Named ClaudeManager for drop-in compatibility."""

    def __init__(self, model: str = MODEL):
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "GOOGLE_API_KEY not set. Add it to your .env file."
            )
        self.model = model
        genai.configure(api_key=api_key)
        self.client = genai.GenerativeModel(self.model)
        logger.info("GeminiManager initialised (model=%s)", self.model)

    # ── Public interface ──────────────────────────────────────────────────

    def call(self, prompt: str, use_cache: bool = True, max_tokens: int = 1024) -> str:
        ck = self._cache_key(prompt)

        if use_cache:
            cached = self._get_cached(ck)
            if cached:
                return cached

        logger.info("Gemini API call: model=%s prompt_len=%d", self.model, len(prompt))
        response = self.client.generate_content(prompt)
        result = response.text

        if use_cache:
            self._save_cache(ck, result, prompt)

        return result

    # ── Cache helpers ─────────────────────────────────────────────────────

    def _cache_key(self, prompt: str) -> str:
        return hashlib.md5(f"{self.model}:{prompt}".encode()).hexdigest()

    def _get_cached(self, key: str) -> str | None:
        path = CACHE_DIR / f"claude_{key}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            logger.debug("Cache HIT: %s...", key[:12])
            return data["response"]
        return None

    def _save_cache(self, key: str, response: str, prompt: str):
        path = CACHE_DIR / f"claude_{key}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"response": response, "prompt_preview": prompt[:200]},
                f,
                indent=2,
            )
