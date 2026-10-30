"""Helping a member make their watermark: short text suggestions (free, like hooks), and a picture mark made with AI.

A picture mark is a symbol only. Every AI picture is made with no letters on purpose (models garble writing), so words belong in
the text watermark, which is drawn as real text. A mark costs a picture (2 credits) and, when its background is taken away so it
sits cleanly on any picture, a background removal (1 credit).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from app.billing import credits
from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.prompts.registry import load_prompt
from app.prompts.safe import guard_output
from app.shared.brand_name import brand_display_name
from app.shared.heavy_work import run_heavy
from app.shared.llm import call_llm_structured
from app.shared.storage import ContentType as UploadContentType, upload_file

logger = logging.getLogger(__name__)

MAX_SUGGESTION_CHARS = 28
MARK_SIZE = (1024, 1024)


def usable_suggestions(result: object, *, source: str = "") -> list[str]:
    """Up to 5 short, clean, different suggestions."""
    raw = result.get("suggestions") if isinstance(result, dict) else None
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = guard_output(item, source=source, json_keys=())
        if not text:
            continue
        text = " ".join(text.strip().strip('"').replace("—", " ").replace("#", "").split())
        if not text or len(text) > MAX_SUGGESTION_CHARS or len(text.split()) > 4:
            continue
        if text.casefold() not in {o.casefold() for o in out}:
            out.append(text)
    return out[:5]


async def suggest_texts(brand: dict) -> list[str]:
    from app.pipelines.text.brand_context import build_brand_context

    brand_context = build_brand_context(brand)
    existing = ((brand.get("watermark") or {}).get("text") or "").strip()
    prompt = load_prompt("brand/watermark_suggest", brand_context=brand_context, existing=existing)
    result = await call_llm_structured(prompt, max_tokens=400)
    suggestions = usable_suggestions(result, source=brand_context)
    if not suggestions:
        raise HTTPException(status_code=502, detail="Couldn't come up with suggestions right now. Try again, or type your own.")
    return suggestions


def mark_prompt(brand: dict, idea: str) -> str:
    """What the picture service is asked for: one simple symbol, on plain white so its background can be taken away cleanly."""
    name = brand_display_name(brand) or "this brand"
    idea = " ".join((idea or "").split())[:300]
    about = f" The idea: {idea}." if idea else ""
    return (
        f"A simple flat emblem or symbol for {name}.{about} One clean bold shape that still reads at a very small size, in one or "
        "two solid colours, centred on a plain pure white background with lots of empty space around it. Minimal vector style, "
        "no shadows, no gradients, no scenery. No letters, words, numbers or text of any kind."
    )


async def make_mark(*, brand: dict, idea: str, workspace_id: str, user_id: str) -> MediaAsset:
    """A new picture mark, with its background taken away when that works, saved to the workspace's library."""
    from app.agents.content_guard.media import assert_image_ok
    from app.pipelines.media import background_removal
    from app.pipelines.media.image_generation import generate_image_from_prompt
    from app.shared.entitlements import picture_room_message

    # the same daily picture allowance as every other AI picture
    full = await picture_room_message(workspace_id)
    if full:
        raise HTTPException(status_code=429, detail=full)

    async with credits.spend(workspace_id, user_id, "picture", step="watermark_mark") as hold:
        picture: Optional[bytes] = await generate_image_from_prompt(
            prompt=mark_prompt(brand, idea), workspace_id=workspace_id, user_id=user_id, target_size=MARK_SIZE, brand_profile=brand,
        )
        hold.nothing_made = not picture
    if not picture:
        raise HTTPException(status_code=502, detail="Couldn't make a mark right now. Try again in a moment, or upload your own.")

    async with credits.spend(workspace_id, user_id, "remove_background", step="watermark_cutout") as hold:
        cutout = await run_heavy(background_removal.cut_out, picture)
        hold.nothing_made = not cutout
    data, mime = (cutout, "image/png") if cutout else (picture, "image/jpeg")
    note = None if cutout else "The background couldn't be taken away, so this mark sits on a white square."

    await assert_image_ok(data, mime, workspace_id=workspace_id, where="watermark")
    url = await upload_file(data, UploadContentType.IMAGE, user_id)
    asset = MediaAsset(
        id=str(uuid4()), workspace_id=workspace_id, kind=MediaKind.IMAGE, url=url, mime_type=mime,
        width=MARK_SIZE[0], height=MARK_SIZE[1], source=MediaSource.AI_GENERATED, created_by=user_id,
        created_at=datetime.now(timezone.utc), qa_flagged=note is not None, qa_flag_reason=note,
    )
    await media_assets.insert_one(asset.model_dump())
    return asset
