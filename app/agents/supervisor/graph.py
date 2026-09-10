"""The workspace-supervisor LangGraph — compiled once at import.

    build_digest → reason → synthesize → persist → END

``run_supervisor`` is the single entry point used by the reasoning tick and by
the on-demand ``/supervisor/run`` route.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from app.agents.supervisor import nodes
from app.agents.supervisor.state import SupervisorState, build_initial_state
from app.core.tracing import ainvoke_traced
from app.db.mongo import workspace_flags, workspace_insights

logger = logging.getLogger(__name__)


def build_supervisor_graph():
    g = StateGraph(SupervisorState)
    g.add_node("build_digest", nodes.build_digest_node)
    g.add_node("reason", nodes.reason_node)
    g.add_node("synthesize", nodes.synthesize_node)
    g.add_node("persist", nodes.persist_node)

    g.set_entry_point("build_digest")
    g.add_edge("build_digest", "reason")
    g.add_edge("reason", "synthesize")
    g.add_edge("synthesize", "persist")
    g.add_edge("persist", END)
    return g.compile()


supervisor_graph = build_supervisor_graph()


async def run_supervisor(
    workspace_id: str,
    *,
    events: list[dict],
    signals: list[dict],
    workspace: dict,
    active_members: int,
    open_flags: list[dict],
    trigger: str = "scheduled",
) -> dict:
    """Run one reasoning pass for a workspace. Returns the final state."""
    state = build_initial_state(
        workspace_id,
        events=events,
        signals=signals,
        workspace=workspace,
        active_members=active_members,
        open_flags=open_flags,
        trigger=trigger,
    )
    try:
        result, run_url = await ainvoke_traced(
            supervisor_graph, state,
            run_name="supervisor_pass",
            agent="supervisor",
            workspace_id=workspace_id,
            extra_tags=[f"trigger:{trigger}"],
            metadata={"trigger": trigger},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("run_supervisor failed for ws=%s: %s", workspace_id, exc, exc_info=True)
        return {"workspace_id": workspace_id, "errors": [str(exc)],
                "persisted": {"insight_ids": [], "flag_ids": [], "notification_ids": []}}

    # Stamp the trace URL onto whatever this pass produced, so "why did it flag
    # / recommend this" is one click from the admin UI.
    if run_url:
        result["langsmith_run_url"] = run_url
        p = result.get("persisted") or {}
        if p.get("insight_ids"):
            await workspace_insights.update_many(
                {"_id": {"$in": p["insight_ids"]}}, {"$set": {"langsmith_run_url": run_url}}
            )
        if p.get("flag_ids"):
            await workspace_flags.update_many(
                {"_id": {"$in": p["flag_ids"]}}, {"$set": {"langsmith_run_url": run_url}}
            )
    return result
