"""Something shared into Ekavo from the phone's share sheet (a link, some text, or both) saved as a plain draft.

It is the free path: no model is used and no credit is spent. The words are saved as the shared text with the link under it, as a draft the member can edit and approve later;
nothing is posted. The link is kept as text and never opened here (following a link is the Sources feature, which has its own checks)."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Optional

from app.sources import flow
from app.sources.service import SourceError

MAX_TEXT = 3000
MAX_URL = 2000
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

RunTool = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def first_link(*parts: Optional[str]) -> str:
    """The link that was shared: the explicit one, or the first web address found in the text (many apps put the link inside the text)."""
    for part in parts:
        match = _URL.search(part or "")
        if match:
            return match.group(0).rstrip(".,;)")[:MAX_URL]
    return ""


def draft_text(title: str, text: str, url: str) -> str:
    """The saved words: the title, the shared text without a repeat of the link, then the link."""
    body = (text or "").strip()
    if url:
        body = body.replace(url, "").strip()
    pieces = [p for p in ((title or "").strip(), body, url) if p]
    seen: list[str] = []
    for piece in pieces:                                  # apps often send the same words as title and text
        if piece not in seen and not any(piece in other for other in seen):
            seen.append(piece)
    return "\n\n".join(seen)[:MAX_TEXT]


async def save(workspace_id: str, user_id: str, body: dict[str, Any], *, run_tool: Optional[RunTool] = None) -> dict[str, Any]:
    """Save what was shared as a draft for one brand and platform. Raises `SourceError` with a plain message when nothing was shared or the draft was refused."""
    from app.db.mongo import get_db

    url = first_link(body.get("url"), body.get("text"), body.get("title"))
    text = draft_text(str(body.get("title") or ""), str(body.get("text") or ""), url)
    if not text:
        raise SourceError("empty", "Nothing was shared. Share a link or some text to Ekavo and try again.", 422)
    brand = await get_db()["brand_profiles"].find_one({"id": body.get("brand_id"), "workspace_id": workspace_id}, {"_id": 0, "id": 1})
    if brand is None:
        raise SourceError("brand_unknown", "Choose one of this workspace's brands.", 422)
    platform = str(body.get("platform") or "").strip()
    if not 0 < len(platform) <= 30:
        raise SourceError("input_invalid", "Choose a platform.", 422)
    if run_tool is None:
        from app.sources import runner

        run_tool = runner.tool_runner(workspace_id, user_id)
    try:
        answer = await run_tool("library.create_draft", {"platform": platform, "brand_id": brand["id"], "content": text})
    except flow.SourceToolError as exc:
        raise SourceError(exc.code, exc.message, 403 if exc.stop else 422) from exc
    data = answer.get("data") if isinstance(answer, dict) else None
    piece = None
    if isinstance(data, dict):
        for pool in [data, *[v for v in data.values() if isinstance(v, dict)]]:
            for key in ("piece_id", "id"):
                if isinstance(pool.get(key), str):
                    piece = piece or pool[key]
    return {"saved": True, "piece_id": piece, "link": url or None}
