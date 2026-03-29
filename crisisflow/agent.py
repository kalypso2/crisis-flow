"""
CrisisFlow ADK — Weather/Disaster Agent tree definition.

Architecture:
  root_agent (SequentialAgent)
  ├── data_prep_agent         — MCP: loads disaster events from Snowflake
  ├── parallel_analysis       — ParallelAgent
  │   ├── weather_context     — MCP: Open-Meteo forecast + history
  │   └── impact_analysis     — GoogleSearch: population, infrastructure
  ├── risk_assessment_loop    — LoopAgent (max 3) for self-correction
  │   ├── risk_validator      — checks impact estimates for reasonableness
  │   └── risk_corrector      — re-analyzes failures or calls exit_loop
  ├── enrichment_agent        — writes AI analysis back to Snowflake
  └── briefing_agent          — intelligence briefing + A2A handoff
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, SequentialAgent, ParallelAgent, LoopAgent
from google.adk.tools import exit_loop as adk_exit_loop
from google.adk.tools.google_search_tool import GoogleSearchTool
from mcp import StdioServerParameters

from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset

from .tools import (
    validate_risk_assessment,
    save_disaster_enrichment,
    send_a2a_briefing,
)

MODEL = "gemini-2.5-flash-lite"

# ═══════════════════════════════════════════════════════════════════════════
# 1. Data Prep — loads disaster events from Snowflake + nearby events via MCP
# ═══════════════════════════════════════════════════════════════════════════

data_prep_agent = LlmAgent(
    name="DisasterDataPrepAgent",
    model=MODEL,
    instruction=(
        "You are a disaster intelligence data analyst for CrisisFlow.\n\n"
        "Your job: load unenriched disaster events of type '{event_type}' "
        "from the Snowflake database.\n\n"
        "STEP 1: Call query_disaster_events with event_type='{event_type}' "
        "and limit=5.\n\n"
        "STEP 2: For EACH event returned, call get_nearby_disasters with "
        "that event's lat/lon to detect compound risks (other disasters "
        "within 500km in the last 72 hours).\n\n"
        "STEP 3: Output ONLY a valid JSON object (no extra text before or "
        "after) summarizing what you found:\n"
        '{{"event_type":"{event_type}","events":[{{"id":"...","title":"...",'
        '"lat":0,"lon":0,"severity":0,"timestamp":"...","source":"...",'
        '"location_name":"...","nearby_count":0}},...]}}\n\n'
        "Include ALL events from step 1. The JSON MUST start with {{ and "
        "end with }}. Do NOT wrap it in markdown code fences."
    ),
    tools=[
        MCPToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command="python",
                    args=["-m", "crisisflow_mcp.server"],
                )
            )
        ),
    ],
    output_key="event_data",
)

# ═══════════════════════════════════════════════════════════════════════════
# 2. Parallel Analysis — weather context + impact analysis
# ═══════════════════════════════════════════════════════════════════════════

weather_context_agent = LlmAgent(
    name="WeatherContextAgent",
    model=MODEL,
    instruction=(
        "You are a meteorological analyst for CrisisFlow.\n\n"
        "For each disaster event in {event_data}, use the MCP tools to get "
        "weather data at the event's coordinates:\n\n"
        "1. Call get_weather_forecast(lat, lon) for current conditions and "
        "7-day outlook.\n"
        "2. Call get_weather_history(lat, lon, start_date, end_date) for "
        "the 7 days leading up to the event (use event timestamp minus 7 "
        "days as start_date, event date as end_date, format YYYY-MM-DD).\n\n"
        "After getting weather data for ALL events, output a JSON array:\n"
        '[{{"event_id":"...","current_conditions":{{"temp_c":0,'
        '"wind_kmh":0,"precipitation_mm":0,"description":"..."}},'
        '"forecast_risk":"low|moderate|high|extreme",'
        '"forecast_summary":"1-2 sentence outlook",'
        '"historical_context":"1-2 sentence trend"}},...]\n\n'
        "The forecast_risk should reflect whether upcoming weather will "
        "WORSEN the disaster situation (e.g., more rain for a flood, high "
        "winds for a wildfire, aftershock-prone weather for earthquakes).\n\n"
        "Return ONLY the JSON array."
    ),
    tools=[
        MCPToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command="python",
                    args=["-m", "crisisflow_mcp.server"],
                )
            )
        ),
    ],
    output_key="weather_analysis",
)

impact_analysis_agent = LlmAgent(
    name="ImpactAnalysisAgent",
    model=MODEL,
    instruction=(
        "You are a disaster impact analyst for CrisisFlow.\n\n"
        "For each disaster event in {event_data}, use Google Search to "
        "research:\n"
        "1. Population density near the event coordinates.\n"
        "2. Critical infrastructure at risk (hospitals, roads, power).\n"
        "3. Historical precedent — similar events in the same area.\n"
        "4. Any news reports about this specific event.\n\n"
        "For each event, search using the event title and location name.\n\n"
        "Output a JSON array:\n"
        '[{{"event_id":"...","estimated_affected_population":0,'
        '"infrastructure_at_risk":["hospitals","roads",...],'
        '"historical_precedent":"1-2 sentence comparison",'
        '"news_headline":"exact headline if found",'
        '"news_url":"https://real-news-site.com/article",'
        '"impact_severity":"low|moderate|high|critical|extreme"}},...]\n\n'
        "RULES:\n"
        "- Only use real, verified population numbers from search results.\n"
        "- If you cannot find population data, estimate based on the "
        "location (urban vs rural, country population density).\n"
        "- news_url MUST be a real article URL, NOT a google redirect.\n"
        "- Process ALL events, not just some.\n\n"
        "Return ONLY the JSON array."
    ),
    tools=[GoogleSearchTool(bypass_multi_tools_limit=True)],
    output_key="impact_analysis",
)

aid_context_agent = LlmAgent(
    name="AidContextAgent",
    model=MODEL,
    instruction=(
        "You are a humanitarian logistics specialist for CrisisFlow.\n\n"
        "For each disaster event in {event_data}, determine the real-world "
        "aid response requirements and depot availability:\n\n"
        "STEP 1: Call get_depot_inventory() once to see current UNHRD stock "
        "levels at all 6 global depots.\n\n"
        "STEP 2: For EACH event, call get_nearest_depots(lat, lon, top_n=3) "
        "to identify the 3 closest depots and their air ETAs.\n\n"
        "STEP 3: For EACH event, call calculate_sphere_needs(event_type, "
        "severity, affected_population) to compute Sphere-standard aid "
        "quantities (shelter kits, food rations, medical kits, water kits, "
        "vehicles) per the Sphere Handbook 2018.\n\n"
        "STEP 4: For the country where each event occurred, call "
        "get_ocha_funding(country_name) to retrieve real historical "
        "humanitarian funding flows for that country from OCHA FTS.\n\n"
        "Output a JSON array with one entry per event:\n"
        '[{{"event_id":"...","nearest_depots":[{{"name":"...","distance_km":0,'
        '"air_eta_hours":0,"stock_status":"high|medium|low|critical"}}],'
        '"sphere_needs":{{"shelter_kits":0,"food_rations":0,"medical_kits":0,'
        '"water_kits":0,"vehicles":0,"displaced":0,"window_days":0}},'
        '"ocha_funding_usd":0,"ocha_flow_count":0,'
        '"recommended_depot":"name of best depot",'
        '"logistics_notes":"1-2 sentences on feasibility"}},...]\n\n'
        "Return ONLY the JSON array."
    ),
    tools=[
        MCPToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command="python",
                    args=["-m", "crisisflow_mcp.server"],
                )
            )
        ),
    ],
    output_key="aid_context",
)

parallel_analysis = ParallelAgent(
    name="ParallelAnalysis",
    sub_agents=[weather_context_agent, impact_analysis_agent, aid_context_agent],
)

# ═══════════════════════════════════════════════════════════════════════════
# 3. Risk Assessment Loop — validate and self-correct
# ═══════════════════════════════════════════════════════════════════════════

risk_validator = LlmAgent(
    name="RiskValidator",
    model=MODEL,
    instruction=(
        "You are a quality assurance specialist for disaster risk assessments.\n\n"
        "Review the weather analysis in {weather_analysis} and impact "
        "analysis in {impact_analysis} against the event data in "
        "{event_data}.\n\n"
        "Call validate_risk_assessment to run programmatic checks. The tool "
        "will verify:\n"
        "- Population estimates are within plausible ranges\n"
        "- Weather risk levels match the event type\n"
        "- All events have been analyzed\n"
        "- Impact severity aligns with event severity\n\n"
        "The tool returns a pass/fail result with details."
    ),
    tools=[validate_risk_assessment],
    output_key="validation_result",
)

risk_corrector = LlmAgent(
    name="RiskCorrector",
    model=MODEL,
    instruction=(
        "You are a risk assessment correction specialist.\n\n"
        "The validation result is: {validation_result}\n\n"
        "If ALL checks passed, call exit_loop immediately.\n\n"
        "If any checks FAILED, re-analyze the failed events:\n"
        "- Use Google Search to find better population/impact data.\n"
        "- Adjust impact_severity to match the evidence.\n"
        "- Then call exit_loop (corrections will be applied automatically).\n\n"
        "Always call exit_loop when done."
    ),
    tools=[
        GoogleSearchTool(bypass_multi_tools_limit=True),
        adk_exit_loop,
    ],
    output_key="correction_status",
)

risk_assessment_loop = LoopAgent(
    name="RiskAssessmentLoop",
    sub_agents=[risk_validator, risk_corrector],
    max_iterations=3,
)

# ═══════════════════════════════════════════════════════════════════════════
# 4. Enrichment — save everything to Snowflake
# ═══════════════════════════════════════════════════════════════════════════

enrichment_agent = LlmAgent(
    name="EnrichmentAgent",
    model=MODEL,
    instruction=(
        "You are a data persistence specialist for CrisisFlow.\n\n"
        "Your available tool is EXACTLY: save_disaster_enrichment. "
        "No other tool name exists.\n\n"
        "You MUST call save_disaster_enrichment (no arguments needed — it "
        "reads all data from session state automatically).\n\n"
        "This tool writes the weather context, impact analysis, and "
        "enrichment summaries back to Snowflake for each event.\n\n"
        "STEP 1: Call save_disaster_enrichment.\n"
        "STEP 2: Output a text summary of how many events were enriched.\n\n"
        "IMPORTANT: You MUST call the tool. Without it, no data is saved."
    ),
    tools=[save_disaster_enrichment],
    output_key="enrichment_results",
)

# ═══════════════════════════════════════════════════════════════════════════
# 5. Briefing + A2A handoff
# ═══════════════════════════════════════════════════════════════════════════

briefing_agent = LlmAgent(
    name="BriefingAgent",
    model=MODEL,
    instruction=(
        "You are an intelligence briefing writer for CrisisFlow.\n\n"
        "Using ALL the analysis from this session:\n"
        "- Event data: {event_data}\n"
        "- Weather analysis: {weather_analysis}\n"
        "- Impact analysis: {impact_analysis}\n"
        "- Aid context: {aid_context}\n"
        "- Enrichment results: {enrichment_results}\n\n"
        "Generate a DETAILED intelligence briefing (at least 600 characters) "
        "in this format:\n\n"
        "CRISISFLOW DISASTER INTELLIGENCE BRIEFING\n"
        "==========================================\n"
        "Event Type: {event_type}\n"
        "Generated: [current date/time]\n\n"
        "PRIORITY EVENTS:\n"
        "[For each event, ordered by severity:]\n"
        "- Event title, location, severity\n"
        "- Current weather conditions and forecast risk\n"
        "- Estimated affected population and displaced persons\n"
        "- Infrastructure at risk\n"
        "- Compound risks (nearby disasters)\n\n"
        "AID REQUIREMENTS (Sphere Handbook 2018):\n"
        "[For each priority event:]\n"
        "- Shelter kits needed, food rations, medical kits, water kits\n"
        "- Recommended depot and air ETA\n"
        "- OCHA FTS historical funding context for the country\n"
        "- Depot stock status (high/medium/low/critical)\n\n"
        "RECOMMENDED ACTIONS:\n"
        "[Specific actionable recommendations with named depots and ETAs]\n\n"
        "WEATHER OUTLOOK:\n"
        "[How weather will affect the situation over the next 7 days]\n\n"
        "After generating the briefing text, call send_a2a_briefing with "
        "the full briefing text.\n\n"
        "Then output the FULL briefing text as your response (at least 600 "
        "characters long). Do NOT output just a status summary."
    ),
    tools=[send_a2a_briefing],
    output_key="final_briefing",
)

# ═══════════════════════════════════════════════════════════════════════════
# Root — Sequential orchestration
# ═══════════════════════════════════════════════════════════════════════════

root_agent = SequentialAgent(
    name="CrisisFlowDisasterAnalyst",
    description=(
        "CrisisFlow disaster intelligence analyst. Enriches weather and "
        "natural disaster events with AI-generated impact assessments, "
        "weather context, compound risk detection, and actionable response "
        "plans using real-time data from USGS, NOAA, GDACS, EONET, and "
        "Open-Meteo."
    ),
    sub_agents=[
        data_prep_agent,
        parallel_analysis,
        risk_assessment_loop,
        enrichment_agent,
        briefing_agent,
    ],
)
