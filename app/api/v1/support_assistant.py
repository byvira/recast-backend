"""The support assistant a member chats with, its saved history, and the live
status panel.

The assistant remembers the conversation, answers from the help guides and
from read-only facts about the member's own workspace (connected accounts,
paused generation, recent failures), and hands off to a ticket when it cannot
help. It has no tools and cannot change anything.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.db.mongo import get_client, support_chats, support_incidents, workspace_connections
from app.shared import support_guides
from app.shared.support_log import support_request_log
from app.shared.llm import GroqModel, call_llm_chat, get_circuit_breaker_status, set_usage_workspace
from app.shared.support_ai import diagnose, redact
from app.shared.support_context import build_snapshot

router = APIRouter(dependencies=[Depends(support_request_log)])
logger = logging.getLogger(__name__)

MAX_CHATS_PER_MEMBER = 100
MAX_MESSAGES_PER_CHAT = 60
HISTORY_TURNS = 10  # messages sent back to the model on each turn

_SYSTEM = (
    "You are the Recast support assistant. Recast is a content workspace: a member describes an idea "
    "once and Recast turns it into platform-ready posts in that workspace's own brand voice, across "
    "text, audio and image work, with review before anything publishes.\n\n"
    "How to answer:\n"
    "- Use only the facts about this workspace and the guides below, plus the conversation. If the answer "
    "is not there, say plainly that you do not know and suggest filing a ticket so a person can look. "
    "Never invent a setting, a number, a date, an error code or a feature.\n"
    "- Never promise a fix, a refund or a date.\n"
    "- Be warm, short and plain. No technical jargon. Do not use dashes to join sentences.\n"
    "- If the facts below show a likely cause (for example an account that needs reconnecting), say so first.\n"
    "- You cannot change anything in the account. Do not offer to.\n"
    "- Do not include links or email addresses.\n"
)


class AssistRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    chat_id: Optional[str] = Field(default=None, max_length=80)


def _title(message: str) -> str:
    text = " ".join(message.split())
    return text if len(text) <= 60 else text[:57].rstrip() + "..."


async def _own_chat(chat_id: str, ctx: WorkspaceContext) -> dict:
    chat = await support_chats.find_one(
        {"id": chat_id, "user_id": ctx.user_id, "workspace_id": ctx.workspace_id}
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found.")
    return chat


def _summary(chat: dict) -> dict:
    return {
        "id": chat["id"],
        "title": chat["title"],
        "message_count": len(chat.get("messages", [])),
        "updated_at": chat["updated_at"],
        "created_at": chat["created_at"],
    }


@router.get("/chats")
@limiter.limit("60/minute")
async def list_chats(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    rows = (
        await support_chats.find({"user_id": ctx.user_id, "workspace_id": ctx.workspace_id})
        .sort("updated_at", -1)
        .to_list(limit)
    )
    return {"chats": [_summary(r) for r in rows]}


@router.get("/chats/{chat_id}")
@limiter.limit("60/minute")
async def get_chat(request: Request, chat_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    chat = await _own_chat(chat_id, ctx)
    return {**_summary(chat), "messages": chat.get("messages", [])}


@router.delete("/chats/{chat_id}")
@limiter.limit("30/minute")
async def delete_chat(request: Request, chat_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    await _own_chat(chat_id, ctx)
    await support_chats.delete_one({"id": chat_id})
    return {"deleted": True}


async def _workspace_facts(ctx: WorkspaceContext) -> list[str]:
    """Read-only, plain-language facts about the member's own workspace."""
    try:
        pseudo = {
            "workspace_id": ctx.workspace_id,
            "workspace_name": ctx.workspace.get("name", ""),
            "created_by": ctx.user_id,
            "created_by_name": ctx.user.get("name", ""),
            "category": None,
        }
        snapshot = await build_snapshot(pseudo)
        return [f["text"] for f in await diagnose(pseudo, snapshot)]
    except Exception:
        logger.debug("Couldn't gather workspace facts for the assistant", exc_info=True)
        return []


@router.post("/assist")
@limiter.limit("15/minute")
async def assist(
    request: Request, body: AssistRequest, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> dict:
    now = datetime.now(timezone.utc)
    chat = await _own_chat(body.chat_id, ctx) if body.chat_id else None
    if chat and len(chat.get("messages", [])) >= MAX_MESSAGES_PER_CHAT:
        raise HTTPException(status_code=409, detail="This chat is full. Start a new chat to keep going.")

    history = (chat or {}).get("messages", [])[-HISTORY_TURNS:]
    guides = support_guides.search(body.message, limit=3)
    facts = await _workspace_facts(ctx)

    system = (
        _SYSTEM
        + "\nFacts about this member's workspace right now:\n"
        + ("\n".join(f"- {f}" for f in facts) if facts else "- No facts available.")
        + "\n\nGuides that may help:\n"
        + ("\n\n".join(f"{g['title']}\n{g['body']}" for g in guides) if guides else "None matched.")
    )
    messages = [{"role": m["role"], "content": m["text"]} for m in history]
    messages.append({"role": "user", "content": redact(body.message)})

    set_usage_workspace(ctx.workspace_id)
    try:
        reply = await call_llm_chat(messages=messages, system=system, model=GroqModel.FAST, max_tokens=500)
    except Exception:
        logger.warning("Support assistant call failed for workspace %s", ctx.workspace_id, exc_info=True)
        raise HTTPException(
            status_code=503, detail="Couldn't reach the assistant right now. Try again, or file a ticket below."
        )
    reply = (reply or "").strip()
    if not reply:
        raise HTTPException(status_code=502, detail="The assistant came back empty. Try again, or file a ticket below.")

    entries = [
        {"role": "user", "text": body.message, "at": now},
        {"role": "assistant", "text": reply, "at": now},
    ]
    if chat:
        await support_chats.update_one(
            {"id": chat["id"]}, {"$push": {"messages": {"$each": entries}}, "$set": {"updated_at": now}}
        )
        chat_id, title = chat["id"], chat["title"]
    else:
        chat_id, title = str(uuid4()), _title(body.message)
        await support_chats.insert_one(
            {
                "id": chat_id, "user_id": ctx.user_id, "workspace_id": ctx.workspace_id,
                "title": title, "messages": entries, "created_at": now, "updated_at": now,
            }
        )
        # Keep the history to a sensible size: the oldest go first.
        extra = await support_chats.count_documents({"user_id": ctx.user_id, "workspace_id": ctx.workspace_id}) - MAX_CHATS_PER_MEMBER
        if extra > 0:
            oldest = await support_chats.find(
                {"user_id": ctx.user_id, "workspace_id": ctx.workspace_id}, {"id": 1}
            ).sort("updated_at", 1).to_list(extra)
            await support_chats.delete_many({"id": {"$in": [c["id"] for c in oldest]}})
    return {
        "reply": reply,
        "chat_id": chat_id,
        "title": title,
        "guides": [{"id": g["id"], "title": g["title"]} for g in guides],
    }


# ── Live status ──────────────────────────────────────────────────────────────
_STATE_RANK = {"ok": 0, "notice": 1, "degraded": 2, "down": 3}


@router.get("/status")
@limiter.limit("30/minute")
async def live_status(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """What is working right now, from real checks. No uptime percentage is
    shown because none is measured."""
    components: list[dict] = [
        {"id": "app", "label": "Recast", "state": "ok", "detail": "You are connected."},
    ]

    try:
        await get_client().get_default_database().command("ping")
        components.append({"id": "database", "label": "Saving your work", "state": "ok", "detail": None})
    except Exception:
        components.append({"id": "database", "label": "Saving your work", "state": "down", "detail": "We can't reach our storage right now."})

    breaker = get_circuit_breaker_status()
    components.append(
        {
            "id": "ai",
            "label": "Writing and AI",
            "state": "degraded" if breaker.get("open") else "ok",
            "detail": "Writing is running on a backup and may be slower." if breaker.get("open") else None,
        }
    )

    # The member's own connected accounts, from what Recast stores about them.
    accounts: list[dict] = []
    notes: list[str] = []
    try:
        pseudo = {"workspace_id": ctx.workspace_id, "created_by": ctx.user_id, "created_by_name": ""}
        snapshot = await build_snapshot(pseudo)
        for p in snapshot.get("platforms", []):
            health = p.get("health", "healthy")
            accounts.append(
                {
                    "platform": p.get("platform"),
                    "state": "ok" if health == "healthy" else "notice" if health == "expiring_soon" else "degraded",
                    "detail": {
                        "healthy": None,
                        "expiring_soon": "Will need reconnecting soon.",
                        "expired": "Needs reconnecting.",
                        "disconnected": "Disconnected.",
                    }.get(health),
                }
            )
        if snapshot.get("workspace", {}).get("generation_halted"):
            notes.append("New generation is paused for your workspace by its owner.")
    except Exception:
        logger.debug("Couldn't read the member's connections for the status panel", exc_info=True)
    if accounts:
        worst = max(accounts, key=lambda a: _STATE_RANK[a["state"]])
        components.append(
            {
                "id": "accounts",
                "label": "Your connected accounts",
                "state": worst["state"],
                "detail": None if worst["state"] == "ok" else "One or more accounts need attention.",
                "accounts": accounts,
            }
        )
    else:
        components.append(
            {"id": "accounts", "label": "Your connected accounts", "state": "ok", "detail": "No accounts are connected yet.", "accounts": []}
        )

    incidents = await support_incidents.find({"status": {"$in": ["open", "monitoring"]}}).sort("created_at", -1).to_list(5)
    return {
        "checked_at": datetime.now(timezone.utc),
        "overall": max((c["state"] for c in components), key=lambda s: _STATE_RANK[s]),
        "components": components,
        "incidents": [
            {
                "id": i["id"], "title": i["title"], "status": i["status"],
                "platform": i.get("platform"), "started_at": i["created_at"],
            }
            for i in incidents
        ],
        "notes": notes,
    }
