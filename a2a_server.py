"""
CrisisFlow A2A Server — exposes the Response Coordinator as an
A2A-discoverable agent with an Agent Card at /.well-known/agent.json.

Usage:
    python a2a_server.py                 # default port 8001
    python a2a_server.py --port 8002
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCard,
    AgentSkill,
    AgentCapabilities,
    AgentProvider,
    TaskStatus,
    TaskState,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    Artifact,
    Part,
)

log = logging.getLogger("crisisflow.a2a")


# ── Agent Executor — wraps the ADK Response Coordinator ──────────────────


class ResponseCoordinatorExecutor(AgentExecutor):
    """Bridges A2A protocol requests to the ADK Response Coordinator agent."""

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        # Extract the user's message text
        message = context.get_user_input()
        user_text = ""
        if message and message.parts:
            for part in message.parts:
                if part.HasField("text"):
                    user_text += part.text

        if not user_text:
            event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
                    final=True,
                )
            )
            return

        # Signal that we're working
        event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                ),
            )
        )

        # Run the ADK agent
        try:
            from gemini_manager import configure_for_adk
            configure_for_adk()

            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.genai import types

            from response_coordinator import app as rc_app

            session_service = InMemorySessionService()
            session = await session_service.create_session(
                app_name=rc_app.name,
                user_id="a2a_client",
            )

            runner = Runner(
                app=rc_app,
                session_service=session_service,
            )

            content = types.Content(
                role="user",
                parts=[types.Part(text=user_text)],
            )

            response_text = ""
            async for event in runner.run_async(
                user_id="a2a_client",
                session_id=session.id,
                new_message=content,
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    response_text = event.content.parts[0].text

            # Return the result as an artifact
            event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=Artifact(
                        parts=[Part(text=response_text or "No response generated.")],
                    ),
                )
            )

            event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    final=True,
                )
            )

        except Exception as exc:
            log.error("Agent execution failed: %s", exc, exc_info=True)
            event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                    ),
                    final=True,
                )
            )

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
                final=True,
            )
        )


# ── Build the Agent Card ─────────────────────────────────────────────────


def build_agent_card(host: str, port: int) -> AgentCard:
    return AgentCard(
        name="CrisisFlow Humanitarian Response Coordinator",
        description=(
            "Receives crisis intelligence briefings and generates "
            "actionable humanitarian response plans with structured "
            "alert payloads, resource allocation, and coordination notes."
        ),
        provider=AgentProvider(organization="CrisisFlow"),
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(
                id="response-planning",
                name="Crisis Response Planning",
                description=(
                    "Given a crisis intelligence briefing, generates priority "
                    "actions, resource allocation from 6 global depots, structured "
                    "alert payloads, and coordination notes."
                ),
                tags=["crisis", "humanitarian", "response", "alerts"],
            ),
        ],
    )


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="CrisisFlow A2A Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    card = build_agent_card(args.host, args.port)
    executor = ResponseCoordinatorExecutor()
    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )

    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=handler,
    )

    log.info(
        "CrisisFlow A2A Response Coordinator starting on %s:%d",
        args.host,
        args.port,
    )
    log.info("Agent Card at http://%s:%d/.well-known/agent.json", args.host, args.port)

    uvicorn.run(app.build(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
