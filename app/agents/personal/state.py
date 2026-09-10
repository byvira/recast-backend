"""``PersonaState`` — the contract between personal-assistant graph nodes.

One run of the graph = one observed content piece. The graph loads/creates the
member's persona, embeds the piece, scores it against the voice baseline and the
volume/topic/quality baselines, emits any signals, then folds the piece into the
persona and persists it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from typing_extensions import TypedDict

from app.agents.personal.persona_store import new_persona, persona_id
from app.agents.personal.scope import event_scope


class PersonaState(TypedDict, total=False):
    # ── triggering event, flattened for convenience ────────────────────
    event: dict[str, Any]
    workspace_id: str
    user_id: str
    pipeline_type: Optional[str]
    piece_id: str
    content_text: str
    target: str
    quality_passed: bool
    flagged_for_review: bool

    # ── loaded context ───────────────────────────────────────────────
    persona: dict[str, Any]
    is_new: bool
    history: list[dict]          # recent pieces from the history adapter, newest first

    # ── scoring ─────────────────────────────────────────────────────
    embedding: list[float]
    similarity: float
    baseline_present: bool
    drift_score: float          # combined adaptive-embedding + style score (route: <1 / <2 / >=2)
    style_divergence: float     # register distance from the baseline fingerprint
    drift_route: str            # "in_voice" | "soft" | "judge"
    judge_why: str

    # ── output ─────────────────────────────────────────────────────
    pending_signals: list[dict]      # descriptors consumed by persist_persona
    emitted_signal_ids: list[str]    # signal ids actually written by persist_persona
    now: datetime
    errors: list[str]


def build_initial_state(event: dict[str, Any]) -> PersonaState:
    """Flatten an event envelope into a fresh graph state.

    Raises ``ScopeError`` (via :func:`event_scope`) if the event can't be scoped
    to a workspace + member — the caller must not run the graph in that case.
    """
    workspace_id, user_id = event_scope(event)
    payload = event.get("payload") or {}
    now = datetime.now(timezone.utc)

    return PersonaState(
        event=event,
        workspace_id=workspace_id,
        user_id=user_id,
        pipeline_type=event.get("pipeline_type"),
        piece_id=str(payload.get("content_id") or payload.get("content_ref", {}).get("id") or ""),
        content_text=payload.get("content_text") or "",
        target=payload.get("target") or "",
        quality_passed=bool(payload.get("quality_passed", True)),
        flagged_for_review=bool(payload.get("flagged_for_review", False)),
        persona={},
        is_new=False,
        history=[],
        embedding=[],
        similarity=1.0,
        baseline_present=False,
        drift_score=0.0,
        style_divergence=0.0,
        drift_route="in_voice",
        judge_why="",
        pending_signals=[],
        emitted_signal_ids=[],
        now=now,
        errors=[],
    )


__all__ = ["PersonaState", "build_initial_state", "new_persona", "persona_id"]
