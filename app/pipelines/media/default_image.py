"""Default-image picker.

Gives every generated piece a real default visual with zero extra user action, tried in this order:
  1. A matching brand asset, if the brand has one in its reference library.
  2. A real AI-generated picture (app.pipelines.media.image_generation.generate_brand_image).

When neither gives a picture the post simply has none; a text card is never made in its place, and the member can ask for the
picture again from the post.

Never raises: a failure at any step must not break text generation, which is the actual point of the request.
"""

import logging
from typing import Optional

from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind
from app.pipelines.media.image_generation import generate_brand_image, last_failure_reason

logger = logging.getLogger(__name__)

def _scene_topic(content: str) -> str:
    """What the post is about, for the picture prompt: its opening lines (up to 600 characters), not only the first sentence."""
    return " ".join(ln.strip() for ln in (content or "").splitlines() if ln.strip())[:600]


async def _pick_brand_asset(reference_media_ids: list[str], workspace_id: str) -> Optional[MediaAsset]:
    """Reference-library images are, by definition, real brand assets the
    user curated — any one of them is on-brand. True per-piece topical
    matching (which specific reference photo fits this specific topic)
    would need stored tags/captions on MediaAsset, which don't exist yet —
    a real, honest scope limit, not a faked "smart match"."""
    if not reference_media_ids:
        return None
    doc = await media_assets.find_one({
        "id": {"$in": reference_media_ids},
        "workspace_id": workspace_id,
        "kind": MediaKind.IMAGE.value,
    })
    return MediaAsset(**doc) if doc else None


PICTURE_MODES = ("ai", "brand", "none")


def resolve_picture_mode(*, picture_mode: Optional[str], default_picture: bool, brand_profile: dict) -> str:
    """Which picture a run's posts get: what the run asked for, else "none" when it switched the picture off (older callers send only that), else the
    brand's own default, else an AI picture."""
    if picture_mode in PICTURE_MODES:
        return str(picture_mode)
    if default_picture is False:
        return "none"
    brands_own = (brand_profile.get("visual_identity") or {}).get("default_picture_mode")
    return brands_own if brands_own in PICTURE_MODES else "ai"


async def pick_default_image(
    *, piece_content: str, brand_profile: dict, workspace_id: str, user_id: str, mode: str = "ai",
) -> Optional[MediaAsset]:
    """The single entry point run_text_pipeline calls right after a piece's
    text is final. Never raises. `mode` "none" makes nothing, "brand" only ever uses one of the brand's own
    pictures (no credits), "ai" tries those first and then makes one."""
    if mode == "none":
        return None
    visual_identity = brand_profile.get("visual_identity") or {}
    reference_ids = visual_identity.get("reference_media_ids") or []

    try:
        brand_asset = await _pick_brand_asset(reference_ids, workspace_id)
        if brand_asset:
            return brand_asset
    except Exception as exc:  # noqa: BLE001
        logger.error("Brand-asset match failed for workspace %s: %s", workspace_id, exc)

    if mode == "brand":
        return None                                   # only the brand's own pictures were wanted, and it has none to use
    try:
        topic = _scene_topic(piece_content)
        ai_asset = await generate_brand_image(
            topic=topic, brand_profile=brand_profile,
            workspace_id=workspace_id, user_id=user_id, credit_action="default_picture",
        )
        if ai_asset:
            return ai_asset
    except Exception as exc:  # noqa: BLE001
        logger.error("AI image generation failed for workspace %s: %s", workspace_id, exc)

    # No picture could be made. The post goes out without one, and the reason is in the log; it is never replaced by a text card.
    logger.warning("No default picture for workspace %s: %s", workspace_id, last_failure_reason() or "the picture could not be made")
    return None
