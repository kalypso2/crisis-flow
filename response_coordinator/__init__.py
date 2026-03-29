from google.adk.apps.app import App

from gemini_adk_plugin import GeminiKeyRotationPlugin

from .agent import root_agent

app = App(
    name="response_coordinator",
    root_agent=root_agent,
    plugins=[GeminiKeyRotationPlugin()],
)

__all__ = ["root_agent", "app"]
