"""``SupervisorState`` — contract between workspace-supervisor graph nodes.

One run = one debounced reasoning pass for ONE workspace over a buffered batch
of events + recent assistant signals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from typing_extensions import TypedDict


class SupervisorState(TypedDict, total=False):
    # ── inputs (assembled by the tick before the graph runs) ──────────
    workspace_id: str
    events: list[dict]
    signals: list[dict]
    workspace: dict
    active_members: int
    open_flags: list[dict]
    trigger: str                 # why this pass fired (provenance)
    language: str                 # workspace's resolved language — see ticks.py::_resolve_workspace_language

    # ── working ─────────────────────────────────────────────────────
    digest: dict
    scratchpad: list[dict]       # reason-loop message history
    tool_calls_made: int
    findings: dict               # {"insights": [...], "flags": [...], "notify": bool}

    # ── output ─────────────────────────────────────────────────────
    persisted: dict              # {"insight_ids", "flag_ids", "notification_ids"}
    langsmith_run_url: str
    now: datetime
    errors: list[str]


def build_initial_state(
    workspace_id: str,
    *,
    events: list[dict],
    signals: list[dict],
    workspace: dict,
    active_members: int,
    open_flags: list[dict],
    trigger: str = "scheduled",
    language: str = "en",
) -> SupervisorState:
    return SupervisorState(
        workspace_id=workspace_id,
        events=events or [],
        signals=signals or [],
        workspace=workspace or {},
        active_members=int(active_members or 0),
        open_flags=open_flags or [],
        trigger=trigger,
        language=language,
        digest={},
        scratchpad=[],
        tool_calls_made=0,
        findings={"insights": [], "flags": [], "notify": False},
        persisted={"insight_ids": [], "flag_ids": [], "notification_ids": []},
        langsmith_run_url="",
        now=datetime.now(timezone.utc),
        errors=[],
    )
