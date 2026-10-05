"""
Workspace search — GET /api/v1/search and POST /api/v1/search/agentic.

Two engines, one real candidate pool:
  - GET  /            fast, literal, always on. A case-insensitive match
                       against the workspace's own drafts, presets,
                       campaigns, brand voices, audio and images. No LLM,
                       no budget check — this is what the search box calls
                       on every keystroke, so it has to be cheap and quick.
  - POST /agentic      the member's toggle-on "AI search": the same real
                       pool (widened with a recency fallback so a query
                       like "the one about pricing" can match on meaning,
                       not just the literal word), then one LLM call picks
                       the genuinely relevant ones and says why. Gated by
                       the same kill switch/budget check every other real
                       generation call goes through, and rate-limited
                       tighter than the fast path since it costs tokens.

Every href is a real route into the app — nothing here points at a page
that doesn't exist. Drafts/Library both read the query params this sends
(piece/type/q) to land the member on the exact item, not just the page.
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.agents.supervisor.service import assert_ai_budget_available, assert_generation_allowed
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.db.mongo import (
    audio_assets,
    content_pieces,
    get_brand_profiles_collection,
    get_campaigns_collection,
    image_assets,
    presets,
)
from app.shared.llm import GroqModel, call_llm_structured, set_usage_workspace

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_QUERY_LEN = 200


class SearchItem(BaseModel):
    type: str          # draft | preset | campaign | voice | audio | image
    title: str
    snippet: str = ""
    href: str
    reason: Optional[str] = None


class SearchResponse(BaseModel):
    items: list[SearchItem]


class AgenticSearchRequest(BaseModel):
    query: str


def _safe_regex(q: str) -> re.Pattern:
    return re.compile(re.escape(q.strip()), re.IGNORECASE)


def _snippet(text: str, limit: int = 140) -> str:
    return " ".join((text or "").split())[:limit]


async def _brand_name(brand: dict) -> str:
    from app.shared.brand_name import brand_display_name

    return brand_display_name(brand) or brand.get("brand_type") or "Brand voice"


async def _search_pool(workspace_id: str, pattern: Optional[re.Pattern], per_type: int) -> list[SearchItem]:
    """One real pass over every searchable collection. `pattern` narrows to
    a literal match; None means "most recent N of each type" — used to
    widen the pool for the agentic path so it has something to reason
    over even when the literal query doesn't appear anywhere verbatim."""
    items: list[SearchItem] = []

    piece_filter: dict = {"workspace_id": workspace_id, "deleted": {"$ne": True}}
    if pattern:
        piece_filter["content"] = {"$regex": pattern}
    pieces = await content_pieces.find(
        piece_filter, {"piece_id": 1, "content": 1, "platform": 1, "updated_at": 1},
    ).sort("updated_at", -1).limit(per_type).to_list(length=per_type)
    for p in pieces:
        snippet = _snippet(p.get("content", ""))
        items.append(SearchItem(
            type="draft",
            title=snippet[:60] or "Untitled draft",
            snippet=f"{p.get('platform', '')} draft — {snippet}" if snippet else p.get("platform", ""),
            href=f"/dashboard/drafts?piece={p['piece_id']}",
        ))

    preset_filter: dict = {"workspace_id": workspace_id, "deleted": {"$ne": True}}
    if pattern:
        preset_filter["$or"] = [{"title": {"$regex": pattern}}, {"description": {"$regex": pattern}}]
    preset_docs = await presets.find(
        preset_filter, {"id": 1, "title": 1, "description": 1, "category_label": 1},
    ).sort("updated_at", -1).limit(per_type).to_list(length=per_type)
    for pr in preset_docs:
        items.append(SearchItem(
            type="preset",
            title=pr.get("title") or "Untitled preset",
            snippet=pr.get("description") or pr.get("category_label", ""),
            href=f"/dashboard/presets?q={pr.get('title', '')}",
        ))

    campaign_filter: dict = {"workspace_id": workspace_id}
    if pattern:
        campaign_filter["$or"] = [{"name": {"$regex": pattern}}, {"topic_cluster": {"$regex": pattern}}]
    campaign_docs = await get_campaigns_collection().find(
        campaign_filter, {"id": 1, "name": 1, "topic_cluster": 1},
    ).sort("created_at", -1).limit(per_type).to_list(length=per_type)
    for c in campaign_docs:
        items.append(SearchItem(
            type="campaign",
            title=c.get("name") or "Untitled campaign",
            snippet=_snippet(c.get("topic_cluster", "")),
            href=f"/dashboard/campaigns/{c['id']}",
        ))

    # Brand voices: identity.name is a free-form dict field, not something a
    # regex can reliably target server-side, and a workspace never has more
    # than a handful — filtered in Python instead of at the DB.
    brand_docs = await get_brand_profiles_collection().find(
        {"workspace_id": workspace_id, "is_active": {"$ne": False}},
        {"id": 1, "identity": 1, "brand_type": 1},
    ).limit(20).to_list(length=20)
    for b in brand_docs:
        name = await _brand_name(b)
        if pattern and not pattern.search(name):
            continue
        items.append(SearchItem(type="voice", title=name, snippet="Brand voice profile", href="/dashboard/voices"))

    audio_filter: dict = {"workspace_id": workspace_id}
    if pattern:
        audio_filter["title"] = {"$regex": pattern}
    audio_docs = await audio_assets.find(
        audio_filter, {"id": 1, "title": 1},
    ).sort("created_at", -1).limit(per_type).to_list(length=per_type)
    for a in audio_docs:
        items.append(SearchItem(
            type="audio", title=a.get("title") or "Untitled recording",
            snippet="Audio pipeline", href="/dashboard/library?type=audio&q=" + (a.get("title") or ""),
        ))

    image_filter: dict = {"workspace_id": workspace_id}
    if pattern:
        image_filter["title"] = {"$regex": pattern}
    image_docs = await image_assets.find(
        image_filter, {"id": 1, "title": 1},
    ).sort("created_at", -1).limit(per_type).to_list(length=per_type)
    for i in image_docs:
        items.append(SearchItem(
            type="image", title=i.get("title") or "Untitled image",
            snippet="Image pipeline", href="/dashboard/library?type=image&q=" + (i.get("title") or ""),
        ))

    return items


@router.get("/")
@limiter.limit("120/minute")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=_MAX_QUERY_LEN),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> SearchResponse:
    """The default search: fires on every keystroke (debounced client-side),
    so it stays a plain, fast, real match — never an LLM call."""
    pattern = _safe_regex(q)
    items = await _search_pool(ctx.workspace_id, pattern, per_type=8)
    return SearchResponse(items=items[:24])


_AGENTIC_SYSTEM = (
    "You search one member's own workspace for them. You are given their query "
    "and a numbered list of real items already in their workspace (drafts, "
    "presets, campaigns, brand voices, audio and images). Pick only the items "
    "that genuinely answer the query — by topic, meaning or wording, not just "
    "shared letters. Return fewer items rather than padding with weak matches. "
    "For each pick, write one short, specific reason (under 12 words) naming "
    "what in that item matches."
)


@router.post("/agentic")
@limiter.limit("15/minute")
async def agentic_search(
    request: Request,
    body: AgenticSearchRequest,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> SearchResponse:
    """The member's toggle-on AI search: the LLM reasons over the same real
    items the fast path uses, so a query like "the launch post" can surface
    a draft that never contains the word "launch" verbatim."""
    query = body.query.strip()
    if not query:
        return SearchResponse(items=[])
    if len(query) > _MAX_QUERY_LEN:
        raise HTTPException(status_code=400, detail="That query is too long.")

    await assert_generation_allowed(ctx.workspace_id)
    await assert_ai_budget_available(ctx.workspace_id)

    pattern = _safe_regex(query)
    literal = await _search_pool(ctx.workspace_id, pattern, per_type=10)
    # Nothing matched the literal words at all — widen to the workspace's
    # most recent items so the model has real material to reason about
    # instead of an empty prompt.
    pool = literal if literal else await _search_pool(ctx.workspace_id, None, per_type=8)
    if not pool:
        return SearchResponse(items=[])

    numbered = "\n".join(f"{i}. [{it.type}] {it.title} — {it.snippet}" for i, it in enumerate(pool))
    prompt = (
        f"Member's query: \"{query}\"\n\nWorkspace items:\n{numbered}\n\n"
        'Respond with JSON only: {"matches": [{"index": <int>, "reason": "<short reason>"}]}. '
        "Use only indices from the list above. If nothing genuinely matches, return an empty list."
    )

    set_usage_workspace(ctx.workspace_id)
    try:
        result = await call_llm_structured(prompt, system=_AGENTIC_SYSTEM, model=GroqModel.FAST, max_tokens=600)
    except Exception as exc:
        # Never let a flaky model call break search — the member's toggle
        # can fall back to the plain results the frontend already has.
        logger.warning("Agentic search LLM call failed for workspace %s: %s", ctx.workspace_id, exc)
        return SearchResponse(items=literal[:8])

    matches = result.get("matches") if isinstance(result, dict) else None
    if not isinstance(matches, list):
        return SearchResponse(items=literal[:8])

    picked: list[SearchItem] = []
    seen: set[int] = set()
    for m in matches:
        if not isinstance(m, dict):
            continue
        idx = m.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(pool) or idx in seen:
            continue
        seen.add(idx)
        item = pool[idx]
        reason = m.get("reason")
        picked.append(item.model_copy(update={"reason": reason if isinstance(reason, str) else None}))

    return SearchResponse(items=picked[:10])
