"""
ADK plugin: rotate Gemini API keys before each LLM call.

Loaded by ``crisisflow.app`` and ``response_coordinator.app`` so ``adk web``,
``run_pipeline.py``, and ``a2a_server.py`` all spread quota across ``GEMINI_KEYS``.

Uses an asyncio Lock to serialize LLM calls — critical when ParallelAgent
fires multiple sub-agents that would otherwise burst past RPM limits.

Model fallback: when a key's primary model (gemini-2.5-flash) is
exhausted, the plugin rewrites ``llm_request.model`` to the fallback
(gemini-2.5-flash-lite) transparently — doubling effective RPD per key.

Set ``ADK_PRE_LLM_SLEEP_SECONDS`` (optional, default 3) to space calls apart.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

log = logging.getLogger("crisisflow.gemini_adk_plugin")

_llm_lock = asyncio.Lock()

_ctx_key_idx = contextvars.ContextVar("_ctx_key_idx", default=-1)
_ctx_model = contextvars.ContextVar("_ctx_model", default="")


class GeminiKeyRotationPlugin(BasePlugin):
    """Serialize and rate-limit LLM calls with key rotation + model fallback."""

    def __init__(self) -> None:
        super().__init__(name="gemini_key_rotation")

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ):
        async with _llm_lock:
            delay = float(os.environ.get("ADK_PRE_LLM_SLEEP_SECONDS", "3") or "3")
            if delay > 0:
                await asyncio.sleep(delay)

            from gemini_manager import configure_for_adk, get_manager

            api_key, model = configure_for_adk()

            manager = get_manager()
            idx = manager.keys.index(api_key)
            _ctx_key_idx.set(idx)
            _ctx_model.set(model)

            if model != llm_request.model:
                log.info(
                    "Model fallback: %s -> %s (key #%d …%s, agent=%s)",
                    llm_request.model,
                    model,
                    idx,
                    api_key[-6:] if len(api_key) >= 6 else api_key,
                    callback_context.agent_name,
                )
                llm_request.model = model
            else:
                log.debug(
                    "Gemini key rotation: agent=%s model=%s key #%d …%s",
                    callback_context.agent_name,
                    model,
                    idx,
                    api_key[-6:] if len(api_key) >= 6 else api_key,
                )
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ):
        err = llm_response.error_code or ""
        msg = llm_response.error_message or ""
        err_str = str(err) + msg

        is_429 = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
        is_suspended = "PERMISSION_DENIED" in err_str or "CONSUMER_SUSPENDED" in err_str or "403" in err_str

        idx = _ctx_key_idx.get(-1)
        model = _ctx_model.get("")

        if is_suspended and idx >= 0:
            from gemini_manager import get_manager, PRIMARY_MODEL, FALLBACK_MODEL
            manager = get_manager()
            for m in (PRIMARY_MODEL, FALLBACK_MODEL):
                manager.mark_model_exhausted(idx, m, seconds=86400 * 365)
            log.error(
                "403 PERMISSION_DENIED — key #%d permanently suspended, "
                "marked exhausted for 1 year",
                idx,
            )
        elif is_429 and idx >= 0 and model:
            from gemini_manager import get_manager
            manager = get_manager()
            manager.mark_model_exhausted(idx, model, seconds=3600)
            log.warning(
                "429 detected in response — marked key #%d model %s exhausted",
                idx, model,
            )
        return None
