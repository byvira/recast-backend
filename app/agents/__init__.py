# app/agents/__init__.py
#
# Agents are imported directly by their respective pipeline orchestrators.
# Do not import agent graphs here — it creates circular import chains.
#
# Import pattern used by orchestrators:
#   from app.agents.text.graph import build_single_platform_graph
#
# The video pipeline's agent is the director (app/agents/video/director.py), run by the shared plan executor.