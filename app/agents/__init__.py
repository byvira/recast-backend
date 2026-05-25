# app/agents/__init__.py
#
# Agents are imported directly by their respective pipeline orchestrators.
# Do not import agent graphs here — it creates circular import chains.
#
# Import pattern used by orchestrators:
#   from app.agents.text.graph import build_single_platform_graph
#
# audio_agent, image_agent, video_agent — not yet implemented.
# Add them here when their pipelines are built.