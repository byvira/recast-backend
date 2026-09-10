"""The personal-assistant LangGraph — compiled once at import, reused per event.

    load_persona → embed_piece → compute_signals → aux_signals
        → route_drift ─ in_voice ─→ persist_persona → END
                      ─ soft ─────→ emit_soft ──────→ persist_persona → END
                      ─ judge ────→ judge_drift ────→ persist_persona → END
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from app.agents.personal import nodes
from app.agents.personal.state import PersonaState, build_initial_state
from app.agents.personal.scope import ScopeError
from app.core.tracing import ainvoke_traced

logger = logging.getLogger(__name__)


def build_personal_graph():
    g = StateGraph(PersonaState)

    g.add_node("load_persona", nodes.load_persona_node)
    g.add_node("embed_piece", nodes.embed_piece_node)
    g.add_node("compute_signals", nodes.compute_signals_node)
    g.add_node("aux_signals", nodes.aux_signals_node)
    g.add_node("emit_soft", nodes.emit_soft_node)
    g.add_node("judge_drift", nodes.judge_drift_node)
    g.add_node("persist_persona", nodes.persist_persona_node)

    g.set_entry_point("load_persona")
    g.add_edge("load_persona", "embed_piece")
    g.add_edge("embed_piece", "compute_signals")
    g.add_edge("compute_signals", "aux_signals")
    g.add_conditional_edges(
        "aux_signals",
        nodes.route_drift,
        {"in_voice": "persist_persona", "soft": "emit_soft", "judge": "judge_drift"},
    )
    g.add_edge("emit_soft", "persist_persona")
    g.add_edge("judge_drift", "persist_persona")
    g.add_edge("persist_persona", END)

    return g.compile()


# Compiled once — reused for every event the worker consumes.
personal_graph = build_personal_graph()


async def run_personal_graph(event: dict[str, Any]) -> dict:
    """Entry point used by the arq consumer (and the Stage-1 smoke test).

    Returns the final state on success, or ``{"skipped": reason}`` when the event
    can't be handled — the caller should still ACK it (a malformed event will
    never become valid on redelivery).
    """
    try:
        state = build_initial_state(event)
    except ScopeError as exc:
        logger.warning("personal graph: skipping unscopable event %s: %s", event.get("event_id"), exc)
        return {"skipped": str(exc)}

    pt = event.get("pipeline_type") or "none"
    result, _run_url = await ainvoke_traced(
        personal_graph, state,
        run_name="personal_pass",
        agent="personal",
        workspace_id=state["workspace_id"],
        user_id=state["user_id"],
        extra_tags=[f"pipeline:{pt}"],
        metadata={
            "pipeline_type": event.get("pipeline_type"),
            "event_type": event.get("event_type"),
        },
    )
    return result
