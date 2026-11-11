"""
What has worked for this workspace, as one plain sentence for the writing prompt.

It is a statement of fact about a real post, not a rule or a prediction: "your best performing LinkedIn post so far opened with
...". It needs enough measured posts to mean something, and says nothing otherwise. On unless PERFORMANCE_HINT_IN_PROMPTS is false.
"""

import logging
import re
from typing import Optional

from app.core.config import settings
from app.db.mongo import content_pieces, get_db

logger = logging.getLogger(__name__)

MIN_MEASURED_POSTS = 3


def _engagement(metrics: dict) -> int:
    """What people did with a post. Clicks and saves count too, so a blog or newsletter post whose results were typed in by hand
    (views and clicks, often no likes) can still be the one that worked best."""
    return sum(int(metrics.get(k) or 0) for k in ("likes", "comments", "shares", "reposts", "saves", "clicks"))


def _opening(content: str, limit: int = 110) -> str:
    first = next((ln.strip() for ln in (content or "").splitlines() if ln.strip()), "")
    first = re.sub(r"\s+", " ", first)
    return first[:limit].rstrip()


async def best_post_hint(workspace_id: str, platform: str) -> Optional[str]:
    """One sentence naming how the workspace's best measured post on this platform opened, or None (feature off, not enough
    measured posts, or nothing to compare). Never raises: it is an extra on top of the writing prompt."""
    if not settings.PERFORMANCE_HINT_IN_PROMPTS:
        return None
    try:
        pieces = await content_pieces.find(
            {"workspace_id": workspace_id, "platform": platform, "publish_status": "published", "deleted": {"$ne": True}},
            {"piece_id": 1, "content": 1},
        ).to_list(length=200)
        if len(pieces) < MIN_MEASURED_POSTS:
            return None
        by_id = {p["piece_id"]: p for p in pieces if p.get("piece_id")}
        metrics = await get_db()["post_metrics"].find(
            {"workspace_id": workspace_id, "post_id": {"$in": list(by_id)}}
        ).to_list(length=200)
        measured = {m["post_id"] for m in metrics}  # a post with both a platform read and typed-in results counts once
        if len(measured) < MIN_MEASURED_POSTS:
            return None
        best = max(metrics, key=_engagement)
        if _engagement(best) <= 0:
            return None
        opening = _opening(by_id[best["post_id"]].get("content", ""))
        if not opening:
            return None
        return f"WHAT HAS WORKED HERE: your best performing {platform} post so far (of {len(measured)} measured) opened with: \"{opening}\". Use it as a reference for tone and openers, not something to copy."
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not build the performance hint for %s: %s", workspace_id, exc)
        return None
