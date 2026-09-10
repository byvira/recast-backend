"""The supervisor's investigative tools.

Seven read-only DB tools. Each is a closure bound to a single ``workspace_id``
that comes from the stream partition / authenticated context — it is NOT a tool
parameter and the model never sees it, so the model physically cannot query
another tenant. Every Mongo filter in here begins with ``workspace_id``.

``make_tools(workspace_id)`` returns ``(specs, dispatch)`` where ``specs`` is the
OpenAI/Groq tool schema list and ``dispatch(name, args)`` runs the bound impl and
returns a JSON-serialisable dict.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.core.tracing import tool_run
from app.db.mongo import (
    brand_profiles,
    content_pieces,
    member_personas,
    personal_signals,
    workspace_events,
    workspace_flags,
    workspaces,
)

logger = logging.getLogger(__name__)

ToolFn = Callable[[dict], Awaitable[dict]]


# ── OpenAI/Groq tool schemas (no workspace_id anywhere) ──────────────────────
TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_member_recent_content",
            "description": "Recent content pieces authored by one workspace member, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_persona_summary",
            "description": (
                "A member's voice persona summary: style fingerprint, topics, volume/quality "
                "stats, and recent drift history. Never returns raw embeddings or private "
                "assistant reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_tier_history",
            "description": "This workspace's tier and any recent tier.changed events.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_publishes",
            "description": "content.published events in the workspace over the last N hours.",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 168}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_flags",
            "description": "Currently-open supervisor flags for this workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal_history",
            "description": "Personal-assistant signals in this workspace; optionally filter to one member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_brand_voice_versions",
            "description": "Brand profiles in this workspace and recent brand.voice_updated events.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _iso_since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def make_tools(workspace_id: str) -> tuple[list[dict], ToolFn]:
    """Return ``(TOOL_SPECS, dispatch)`` with every impl bound to *workspace_id*."""

    async def get_member_recent_content(args: dict) -> dict:
        uid = str(args.get("user_id", ""))
        limit = max(1, min(int(args.get("limit", 10) or 10), 25))
        if not uid:
            return {"error": "user_id required"}
        rows = await content_pieces.find(
            {"workspace_id": workspace_id, "user_id": uid, "deleted": {"$ne": True}},
            {"piece_id": 1, "platform": 1, "content": 1, "created_at": 1,
             "quality_passed": 1, "flagged_for_review": 1, "pipeline_type": 1},
        ).sort("created_at", -1).limit(limit).to_list(length=limit)
        return {"user_id": uid, "count": len(rows), "pieces": [
            {"id": r.get("piece_id"), "pipeline_type": r.get("pipeline_type", "text"),
             "target": r.get("platform"), "created_at": str(r.get("created_at")),
             "quality_passed": r.get("quality_passed", True),
             "flagged": r.get("flagged_for_review", False),
             "excerpt": (r.get("content") or "")[:280]}
            for r in rows
        ]}

    async def get_member_persona_summary(args: dict) -> dict:
        uid = str(args.get("user_id", ""))
        if not uid:
            return {"error": "user_id required"}
        doc = await member_personas.find_one({"_id": f"{workspace_id}:{uid}"})
        if not doc:
            return {"user_id": uid, "persona": None}
        voice = dict(doc.get("voice", {}))
        voice.pop("baseline_embedding", None)
        return {
            "user_id": uid,
            "lifetime": doc.get("lifetime", {}),
            "style_fingerprint": doc.get("style_fingerprint", {}),
            "topics_top": sorted(
                doc.get("topics", {}).get("keyword_histogram", {}).items(),
                key=lambda kv: kv[1], reverse=True)[:15],
            "volume_stats": doc.get("volume_stats", {}),
            "quality_stats": doc.get("quality_stats", {}),
            "recent_similarities": voice.get("recent_similarities", []),
            "drift_history": doc.get("drift_history", [])[-15:],
        }

    async def get_workspace_tier_history(_args: dict) -> dict:
        ws = await workspaces.find_one({"id": workspace_id}) or {}
        changes = await workspace_events.find(
            {"workspace_id": workspace_id, "event_type": "tier.changed"}
        ).sort("occurred_at", -1).limit(10).to_list(length=10)
        return {
            "current_tier": ws.get("tier"),
            "seats": (ws.get("tier_config") or {}).get("seats"),
            "changes": [
                {"at": c.get("occurred_at"), **(c.get("payload") or {})} for c in changes
            ],
        }

    async def get_recent_publishes(args: dict) -> dict:
        hours = max(1, min(int(args.get("hours", 24) or 24), 168))
        rows = await workspace_events.find(
            {"workspace_id": workspace_id, "event_type": "content.published",
             "occurred_at": {"$gte": _iso_since(hours)}},
        ).sort("occurred_at", -1).limit(200).to_list(length=200)
        return {"hours": hours, "count": len(rows), "publishes": [
            {"at": r.get("occurred_at"), "actor": r.get("actor_user_id"),
             "pipeline_type": r.get("pipeline_type"),
             "target": (r.get("payload") or {}).get("target")}
            for r in rows
        ]}

    async def get_open_flags(_args: dict) -> dict:
        rows = await workspace_flags.find(
            {"workspace_id": workspace_id, "status": "open"}
        ).sort("created_at", -1).to_list(length=100)
        return {"count": len(rows), "flags": [
            {"id": r.get("_id"), "type": r.get("flag_type"), "severity": r.get("severity"),
             "detection": r.get("detection"), "summary": r.get("summary_persona"),
             "created_at": str(r.get("created_at"))}
            for r in rows
        ]}

    async def get_signal_history(args: dict) -> dict:
        days = max(1, min(int(args.get("days", 7) or 7), 30))
        q: dict[str, Any] = {
            "workspace_id": workspace_id,
            "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(days=days)},
        }
        if args.get("user_id"):
            q["user_id"] = str(args["user_id"])
        rows = await personal_signals.find(q).sort("created_at", -1).limit(200).to_list(length=200)
        return {"days": days, "count": len(rows), "signals": [
            {"user_id": r.get("user_id"), "type": r.get("signal_type"),
             "severity": r.get("severity"), "at": str(r.get("created_at")),
             "note": r.get("supervisor_note")}
            for r in rows
        ]}

    async def get_brand_voice_versions(_args: dict) -> dict:
        profiles = await brand_profiles.find(
            {"workspace_id": workspace_id}, {"id": 1, "brand_type": 1, "updated_at": 1, "is_complete": 1}
        ).to_list(length=50)
        edits = await workspace_events.find(
            {"workspace_id": workspace_id, "event_type": "brand.voice_updated"}
        ).sort("occurred_at", -1).limit(20).to_list(length=20)
        return {
            "profiles": [
                {"id": p.get("id"), "brand_type": p.get("brand_type"),
                 "is_complete": p.get("is_complete"), "updated_at": str(p.get("updated_at"))}
                for p in profiles
            ],
            "recent_edits": [
                {"at": e.get("occurred_at"), **(e.get("payload") or {})} for e in edits
            ],
        }

    impls: dict[str, ToolFn] = {
        "get_member_recent_content": get_member_recent_content,
        "get_member_persona_summary": get_member_persona_summary,
        "get_workspace_tier_history": get_workspace_tier_history,
        "get_recent_publishes": get_recent_publishes,
        "get_open_flags": get_open_flags,
        "get_signal_history": get_signal_history,
        "get_brand_voice_versions": get_brand_voice_versions,
    }

    async def dispatch(name: str, args: dict) -> dict:
        fn = impls.get(name)
        if fn is None:
            return {"error": f"unknown tool {name!r}"}
        # Each tool invocation is its own LangSmith `tool` child run — the name
        # and args are visible, nested under the reasoning model's run.
        traced = tool_run(name)(fn)
        try:
            return await traced(args or {})
        except Exception as exc:  # noqa: BLE001
            logger.error("supervisor tool %s failed: %s", name, exc)
            return {"error": f"{name} failed: {exc}"}

    return TOOL_SPECS, dispatch
