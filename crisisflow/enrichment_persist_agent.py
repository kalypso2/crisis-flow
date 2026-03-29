"""
Non-LLM agent: always runs save_disaster_enrichment once per pipeline.

Replaces a model-driven enrichment step that could skip the tool call.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from typing_extensions import override

from google.adk.agents import BaseAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types

log = logging.getLogger("crisisflow.enrichment_persist")


class EnrichmentPersistAgent(BaseAgent):
    """Persists weather, impact, and aid analysis from session state to Snowflake."""

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        from crisisflow.tools import save_disaster_enrichment

        tc = Context(ctx)
        preview = ""
        try:
            preview = save_disaster_enrichment(tc)
        except Exception:
            log.exception("EnrichmentPersistAgent: save_disaster_enrichment failed")
            preview = "ERROR: enrichment save raised an exception (see server logs)."
        if preview:
            tc.state["enrichment_results"] = preview
        text = (preview or "No enrichment summary returned.")[:8000]
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=text)],
            ),
        )
