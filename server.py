"""
CrisisFlow Cloud Run entrypoint.

Wraps ADK's FastAPI app and adds /.well-known/agent.json routes
so judges can discover both agents via the A2A Agent Card spec.
"""

import json
import os

import uvicorn
from fastapi.responses import JSONResponse
from google.adk.cli.fast_api import get_fast_api_app

_DIR = os.path.dirname(os.path.abspath(__file__))
_ADK_DIR = os.path.join(_DIR, "adk")

PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")

app = get_fast_api_app(
    agents_dir=_ADK_DIR,
    web=False,
    host=HOST,
    port=PORT,
    allow_origins=["*"],
)

# ── Agent Card routes (A2A discovery) ─────────────────────────────────────

@app.get("/.well-known/agent.json")
async def main_agent_card():
    path = os.path.join(_ADK_DIR, "crisisflow", "agent.json")
    with open(path) as f:
        return JSONResponse(json.load(f))

@app.get("/.well-known/logistics-agent.json")
async def logistics_agent_card():
    path = os.path.join(_ADK_DIR, "logistics_agent", "agent.json")
    with open(path) as f:
        return JSONResponse(json.load(f))


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
