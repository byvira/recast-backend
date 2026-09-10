"""Thin service layer the ``/assistant/*`` routes call.

Every function takes ``workspace_id`` + ``user_id`` from the authenticated
request context and scopes on both — a member only ever sees their own persona
and their own signals, even a workspace admin calling these endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.agents.personal import assist as assist_mod
from app.agents.personal.persona_store import load as load_persona, persona_id
from app.db.mongo import member_personas, personal_signals


def _public_persona(doc: dict) -> dict:
    """Persona doc minus the heavy raw embedding — safe to return over the API."""
    voice = dict(doc.get("voice", {}))
    voice.pop("baseline_embedding", None)
    voice["baseline_dim"] = len(doc.get("voice", {}).get("baseline_embedding", []))
    return {
        "workspace_id": doc["workspace_id"],
        "user_id": doc["user_id"],
        "persona_name": doc.get("persona_name", "Remy"),
        "lifetime": doc.get("lifetime", {}),
        "voice": voice,
        "style_fingerprint": doc.get("style_fingerprint", {}),
        "topics": {
            "top_keywords": sorted(
                doc.get("topics", {}).get("keyword_histogram", {}).items(),
                key=lambda kv: kv[1], reverse=True,
            )[:20],
            "window_size": len(doc.get("topics", {}).get("top_30_window_ids", [])),
        },
        "volume_stats": doc.get("volume_stats", {}),
        "quality_stats": doc.get("quality_stats", {}),
        "drift_history": doc.get("drift_history", [])[-25:],
        "updated_at": doc.get("updated_at"),
    }


async def get_persona(workspace_id: str, user_id: str) -> dict:
    doc = await load_persona(workspace_id, user_id)
    if not doc:
        raise HTTPException(
            status_code=404,
            detail="No persona yet — create some content and Remy will start learning your voice.",
        )
    return _public_persona(doc)


async def list_signals(
    workspace_id: str, user_id: str, *, status: Optional[str] = None, limit: int = 50
) -> dict:
    query: dict = {"workspace_id": workspace_id, "user_id": user_id}
    if status:
        query["status"] = status
    cursor = personal_signals.find(query).sort("created_at", -1).limit(min(limit, 200))
    items = []
    async for doc in cursor:
        doc["id"] = doc.pop("_id")
        items.append(doc)
    return {"items": items, "total": len(items)}


async def acknowledge_signal(workspace_id: str, user_id: str, signal_id: str) -> dict:
    res = await personal_signals.update_one(
        {"_id": signal_id, "workspace_id": workspace_id, "user_id": user_id},
        {"$set": {"status": "acknowledged", "resolved_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Signal not found.")
    return {"id": signal_id, "status": "acknowledged"}


async def assist(
    workspace_id: str, user_id: str, *, pipeline_type: Optional[str], draft_text: str, target: str = ""
) -> dict:
    if not draft_text or not draft_text.strip():
        raise HTTPException(status_code=400, detail="draft_text is required.")
    return await assist_mod.align_draft(
        workspace_id=workspace_id,
        user_id=user_id,
        pipeline_type=pipeline_type,
        draft_text=draft_text,
        target=target,
    )


async def nudge(workspace_id: str, user_id: str, *, piece_id: Optional[str] = None) -> dict:
    summary = await assist_mod.cached_nudge(workspace_id, user_id) or {}
    latest = (
        await assist_mod.latest_signal_for_piece(workspace_id, user_id, piece_id)
        if piece_id else None
    )
    in_voice = None
    remy = None
    if latest:
        in_voice = latest.get("signal_type") not in ("voice_drift", "voice_drift_trend")
        remy = latest.get("member_message")
    elif summary.get("recent_similarity_avg") is not None:
        in_voice = True
        remy = "your recent content is tracking your usual voice."
    return {
        "piece_id": piece_id,
        "in_voice": in_voice,
        "remy_message": remy,
        "latest_signal": latest,
        "persona_summary": summary or None,
    }
