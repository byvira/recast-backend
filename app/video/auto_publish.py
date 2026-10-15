"""Letting an automatic recipe publish by itself. The owner chose to allow it, with these limits that nothing bypasses:

* only an owner or admin can switch it on, and it stops the moment that person is no longer an owner or admin;
* every check must pass first (steps worked, voice check, quality gate), and then each post must pass the same schedule checks a person's
  post does: the platform is open, the account is connected, the content gate and the safety check before sending;
* the recipe names the brand and the platforms it may post to, and nothing else;
* at most DAILY_CAP automatic posts a workspace a day;
* each post is scheduled UNDO_MINUTES ahead, not sent at once, and the decision in the Activity Log has Pull them to cancel it.

The posts are ordinary posts, scheduled through the same queue, so the kill switch, the scheduler and the publish checks all apply."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import brand_profiles, content_pieces, get_db

logger = logging.getLogger(__name__)

DAILY_CAP = 3
UNDO_MINUTES = 15
ALLOWED_ROLES = ("owner", "admin")
MAX_TEXT = 600


def can_enable(role: Optional[str]) -> bool:
    return (role or "") in ALLOWED_ROLES


def clean_settings(raw: Optional[dict], role: Optional[str]) -> dict[str, Any]:
    """Validated publish settings: the brand, the platforms and the daily cap (never above DAILY_CAP). Raises ValueError in plain words."""
    from app.models.text import Platform

    if not can_enable(role):
        raise ValueError("Only an owner or admin can let a recipe publish by itself.")
    raw = raw or {}
    if not str(raw.get("brand_id") or "").strip():
        raise ValueError("Choose the brand this recipe posts for.")
    valid = {p.value for p in Platform}
    platforms = [p for p in dict.fromkeys(raw.get("platforms") or []) if p in valid]
    if not platforms:
        raise ValueError("Choose at least one platform for it to post to.")
    cap = max(1, min(DAILY_CAP, int(raw.get("daily_cap") or DAILY_CAP)))
    return {"brand_id": str(raw["brand_id"]), "platforms": platforms[:6], "daily_cap": cap}


def post_text(title: str, words: list[str]) -> str:
    """The post's words: the video's title, then the start of what is said in it, cut at a sentence end where there is one."""
    body = re.sub(r"\s+", " ", " ".join(w for w in words if w)).strip()
    if len(body) > MAX_TEXT:
        cut = body[:MAX_TEXT]
        stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        body = cut[: stop + 1] if stop > MAX_TEXT * 0.5 else cut.rstrip() + "..."
    return f"{title.strip()}\n\n{body}".strip()


async def posted_today(workspace_id: str, now: datetime) -> int:
    return await content_pieces.count_documents({"workspace_id": workspace_id, "auto_publish.at": {"$gte": now - timedelta(hours=24)}})


async def schedule_video_posts(
    recipe: dict, project_ids: list[str], *, user_id: str, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One post per project and platform, scheduled UNDO_MINUTES ahead. Returns {"scheduled": [piece ids], "held": [reasons]}. A post
    that fails a check, or would pass the daily cap, is held with its reason and nothing is made for it."""
    from app.pipelines.publish import attachments
    from app.pipelines.publish.spine import availability_block, check_gate, platform_key, schedule_blocker
    from app.pipelines.text.storage import ensure_session_exists, save_live_piece

    when = now or datetime.now(timezone.utc)
    auto = recipe.get("automation") or {}
    cfg = auto.get("publish") or {}
    ws = recipe["workspace_id"]
    scheduled: list[str] = []
    held: list[str] = []
    brand = await brand_profiles.find_one({"id": cfg.get("brand_id"), "workspace_id": ws})
    if not brand:
        return {"scheduled": [], "held": ["the brand this recipe posts for is gone"]}
    cap = max(1, min(DAILY_CAP, int(cfg.get("daily_cap") or DAILY_CAP)))
    used = await posted_today(ws, when)
    for pid in project_ids:
        proj = await get_db()["video_projects"].find_one({"id": pid, "workspace_id": ws}, {"title": 1, "edit": 1, "last_render": 1})
        if not proj or not (proj.get("last_render") or {}).get("media_id"):
            held.append("a video was not rendered, so it was not posted")
            continue
        words = [w.get("w", "") for w in ((proj.get("edit") or {}).get("captions") or {}).get("words", [])]
        text = post_text(proj.get("title") or "New video", words)
        for platform in cfg.get("platforms") or []:
            if used >= cap:
                held.append(f"the limit of {cap} automatic posts a day was reached")
                continue
            session_id = str(uuid4())
            await ensure_session_exists(session_id=session_id, workspace_id=ws, user_id=user_id, brand_id=brand["id"], source_type="text")
            piece_id = await save_live_piece(
                session_id=session_id, workspace_id=ws, user_id=user_id, brand_id=brand["id"], platform=platform,
                content=text, word_count=len(text.split()), char_count=len(text),
            )
            await content_pieces.update_one({"piece_id": piece_id, "workspace_id": ws}, {"$set": {
                "approval_status": "approved", "campaign_id": None,
                "auto_publish": {"recipe_id": recipe["id"], "approved_by": user_id, "at": when, "undo_until": when + timedelta(minutes=UNDO_MINUTES)},
            }})
            piece = await content_pieces.find_one({"piece_id": piece_id, "workspace_id": ws})
            resolved = await attachments.resolve_asset(ws, "project", asset_id=pid)
            await attachments.attach(piece, ws, user_id, resolved)
            piece = await content_pieces.find_one({"piece_id": piece_id, "workspace_id": ws})
            problem = None
            blocked = await availability_block(platform, ws)
            if blocked:
                problem = getattr(blocked, "message", None) or "the platform is not open to this workspace"
            if not problem:
                sb = await schedule_blocker(piece, ws)
                problem = sb[1] if sb else None
            if not problem:
                gate = check_gate(piece)
                problem = gate.message if gate else None
            if problem:
                held.append(f"{platform}: {problem}")
                continue
            await content_pieces.update_one(
                {"piece_id": piece_id, "workspace_id": ws, "publish_status": {"$nin": ["published", "publishing"]}},
                {"$set": {"publish_status": "queued", "publish_scheduled_at": when + timedelta(minutes=UNDO_MINUTES),
                          "publish_target": platform_key(platform), "publish_attempts": 0, "updated_at": when}},
            )
            scheduled.append(piece_id)
            used += 1
    return {"scheduled": scheduled, "held": list(dict.fromkeys(held))}


async def pull(workspace_id: str, piece_ids: list[str]) -> int:
    """Cancel scheduled automatic posts that have not gone out. Returns how many were pulled."""
    res = await content_pieces.update_many(
        {"workspace_id": workspace_id, "piece_id": {"$in": piece_ids}, "publish_status": "queued"},
        {"$set": {"publish_status": "pending", "publish_scheduled_at": "", "publish_target": "", "publish_attempts": 0,
                  "updated_at": datetime.now(timezone.utc)}},
    )
    return int(res.modified_count)
