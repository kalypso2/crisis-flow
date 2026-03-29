"""
CrisisFlow — Gemini API Manager
Handles key rotation across multiple projects and response caching.
Maximizes value per API call since rate limits are RPD/RPM, not token-based.

Setup:
    In .env (repo root), set comma-separated keys:
    GEMINI_KEYS=AIza_key1,AIza_key2,AIza_key3

    Optional single-key fallback (timestamp_agent.py, google-genai):
    GOOGLE_API_KEY=AIza_single_key
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from itertools import cycle
from pathlib import Path
from threading import Lock

import google.generativeai as genai

logger = logging.getLogger("crisisflow.gemini")

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "cached_responses"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class GeminiManager:
    """Round-robin key rotation with caching and rate limit awareness."""

    def __init__(self, keys: list[str] | None = None, demo_mode: bool = False):
        if keys is None:
            raw = os.environ.get("GEMINI_KEYS", "")
            keys = [k.strip() for k in raw.split(",") if k.strip()]

        if not keys:
            single = os.environ.get("GOOGLE_API_KEY", "").strip()
            if single:
                keys = [single]
            else:
                raise ValueError(
                    "No API keys found. Set GEMINI_KEYS=key1,key2,... "
                    "or GOOGLE_API_KEY=... in .env"
                )

        self.keys = keys
        self.key_cycle = cycle(range(len(keys)))
        self.lock = Lock()
        self.demo_mode = demo_mode

        self.usage = {i: {"calls": 0, "last_call": 0} for i in range(len(keys))}

        logger.info("GeminiManager initialized with %d API keys", len(keys))
        logger.info("Demo mode: %s", demo_mode)

    def _get_next_key(self) -> tuple[int, str]:
        """Get the next API key in rotation, skipping any that are rate-limited."""
        with self.lock:
            for _ in range(len(self.keys)):
                idx = next(self.key_cycle)
                usage = self.usage[idx]

                now = time.time()
                if now - usage["last_call"] < 6:
                    continue

                usage["last_call"] = now
                usage["calls"] += 1
                return idx, self.keys[idx]

            time.sleep(3)
            idx = next(self.key_cycle)
            self.usage[idx]["last_call"] = time.time()
            self.usage[idx]["calls"] += 1
            return idx, self.keys[idx]

    def _cache_key(self, prompt: str, model: str) -> str:
        content = f"{model}:{prompt}"
        return hashlib.md5(content.encode()).hexdigest()

    def _get_cached(self, cache_key: str) -> str | None:
        path = CACHE_DIR / f"{cache_key}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                logger.info("Cache HIT: %s...", cache_key[:12])
                return data["response"]
        return None

    def _save_cache(self, cache_key: str, response: str, prompt: str, model: str):
        path = CACHE_DIR / f"{cache_key}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "response": response,
                    "model": model,
                    "prompt_preview": prompt[:200],
                    "cached_at": time.time(),
                },
                f,
                indent=2,
            )

    def call(
        self,
        prompt: str,
        model: str = "gemini-2.5-flash",
        use_cache: bool = True,
        force_cache: bool = False,
    ) -> str:
        ck = self._cache_key(prompt, model)

        if use_cache or self.demo_mode:
            cached = self._get_cached(ck)
            if cached:
                return cached

        if force_cache:
            raise RuntimeError(f"Cache miss in force_cache mode: {ck[:12]}")

        key_idx, api_key = self._get_next_key()
        logger.info(
            "API call: key #%s, model=%s, prompt_len=%s, total_calls=%s",
            key_idx,
            model,
            len(prompt),
            self.usage[key_idx]["calls"],
        )

        genai.configure(api_key=api_key)
        gen_model = genai.GenerativeModel(model)

        try:
            response = gen_model.generate_content(prompt)
            result = response.text

            if use_cache:
                self._save_cache(ck, result, prompt, model)

            return result

        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                logger.warning("Rate limited on key #%s, trying next key...", key_idx)
                self.usage[key_idx]["last_call"] = time.time() + 30
                return self.call(prompt, model, use_cache, force_cache)
            raise

    def call_with_image(
        self,
        prompt: str,
        image_data: bytes,
        model: str = "gemini-2.5-flash",
    ) -> str:
        key_idx, api_key = self._get_next_key()
        genai.configure(api_key=api_key)
        gen_model = genai.GenerativeModel(model)

        import io

        import PIL.Image

        image = PIL.Image.open(io.BytesIO(image_data))

        response = gen_model.generate_content([prompt, image])
        return response.text

    def get_usage_stats(self) -> dict:
        return {
            f"key_{i}": {
                "calls": self.usage[i]["calls"],
                "last_call_ago": round(time.time() - self.usage[i]["last_call"], 1)
                if self.usage[i]["last_call"] > 0
                else "never",
            }
            for i in range(len(self.keys))
        }

    def pre_warm_cache(self, prompts: list[dict]):
        logger.info("Pre-warming cache with %d prompts...", len(prompts))
        for i, p in enumerate(prompts):
            ck = self._cache_key(p["prompt"], p["model"])
            if self._get_cached(ck):
                logger.info("  [%d/%d] Already cached, skipping", i + 1, len(prompts))
                continue
            logger.info("  [%d/%d] Computing...", i + 1, len(prompts))
            self.call(p["prompt"], p["model"])
            time.sleep(2)
        logger.info("Cache pre-warm complete")


_manager: GeminiManager | None = None


def get_manager(demo_mode: bool = False) -> GeminiManager:
    global _manager
    if _manager is None:
        _manager = GeminiManager(demo_mode=demo_mode)
    return _manager


def configure_for_adk():
    """Configure genai with the next available key for ADK agent runs."""
    manager = get_manager()
    _, api_key = manager._get_next_key()
    genai.configure(api_key=api_key)
    return api_key


def get_next_api_key() -> str:
    """Return the next key in rotation (e.g. for google-genai ``Client(api_key=...)``)."""
    _, api_key = get_manager()._get_next_key()
    return api_key
