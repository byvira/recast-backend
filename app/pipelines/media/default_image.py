"""Default-image picker — Phase 3 / Rows 7 + 13 of the hybrid-media plan.

Gives every generated piece a real default visual with zero extra user
action, tried in this order:
  1. A matching brand asset, if the brand has one in its reference library.
  2. A real AI-generated image via Cloudflare Workers AI / FLUX.1 [schnell]
     (app.pipelines.media.image_generation.generate_brand_image) — a real
     photo/graphic beats a text card when one's available. Free tier:
     10,000 neurons/day, hard block on exhaustion, no surprise billing.
  3. An auto-rendered on-brand "quote card" using the brand's real
     VisualIdentity colors — the always-works final fallback.

Step 2 was paused mid-session (every provider first checked — Gemini Nano
Banana, HF FLUX, Pollinations.ai — turned out to require real payment or a
key/budget this project didn't have credentials for) and later re-enabled
once Cloudflare Workers AI was verified live end-to-end: real image
generated, real Cloudinary upload succeeded, real QA-gate vision check
ran. See the plan's "Stage E provider pivot" and "Stage E unpaused" notes.

Never raises: a failure at any step must not break text generation, which
is the actual point of the request. Total failure degrades to no image,
the same behaviour as before this picker existed — not a broken generation.
"""

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.pipelines.media.image_generation import generate_brand_image, last_failure_reason
from app.shared.storage import ContentType as UploadContentType, upload_file

logger = logging.getLogger(__name__)

CARD_SIZE = (1080, 1080)
# Recast's own dark theme — used when a brand hasn't set visual_identity
# colors yet, so the fallback still looks intentional, not blank/broken.
_DEFAULT_BG = "#16161D"
_DEFAULT_FG = "#FFFFFF"


def _hook_line(content: str) -> str:
    """First non-empty line of the piece — the same line apply_recommended_hook
    (hook_agent.py) writes the winning hook into, so this is the piece's
    real hook, not an invented caption."""
    for line in (content or "").splitlines():
        line = line.strip()
        if line:
            return line[:180]
    return ""


def _scene_topic(content: str) -> str:
    """What the post is about, for the picture prompt: its opening lines (up to 600 characters), not only the first sentence."""
    return " ".join(ln.strip() for ln in (content or "").splitlines() if ln.strip())[:600]


def _safe_color(value: str, fallback: str) -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    try:
        Image.new("RGB", (1, 1), value)
        return value
    except ValueError:
        return fallback


def _wrap_text(draw: "ImageDraw.ImageDraw", text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_quote_card(hook_text: str, visual_identity: dict) -> bytes:
    """Renders a 1080x1080 PNG: the piece's real hook line on a background
    using the brand's real colors.

    Font rendering uses Pillow's built-in default font, not the brand's
    named font family — fonts.heading/body (VisualIdentity) are free-text
    family names with no .ttf files bundled in this repo to actually
    render them. A real, honest gap (same pattern as media.py's
    width/height-on-upload gap), not silently faked with a font that
    isn't actually the brand's — tracked as a follow-up, not a blocker to
    shipping a real default visual today.
    """
    from app.pipelines.media.contrast_check import contrast_ratio
    from app.pipelines.media.image_render import _load_font

    colors = (visual_identity or {}).get("colors") or {}
    fonts = (visual_identity or {}).get("fonts") or {}
    bg = _safe_color(colors.get("primary", ""), _DEFAULT_BG)
    fg = _safe_color(colors.get("accent", ""), _DEFAULT_FG)
    try:
        if contrast_ratio(fg, bg) < 4.5:  # the brand's accent can be too close to its background to read
            fg = "#FFFFFF" if contrast_ratio("#FFFFFF", bg) >= contrast_ratio("#111111", bg) else "#111111"
    except Exception:  # noqa: BLE001
        fg = _DEFAULT_FG

    img = Image.new("RGB", CARD_SIZE, bg)
    draw = ImageDraw.Draw(img)
    margin = 120
    max_width = CARD_SIZE[0] - margin * 2
    text = (hook_text or "").strip()
    font = _load_font(fonts.get("heading"), 72, bold=True, text=text)
    lines: list[str] = []
    size = 72
    while text:
        font = _load_font(fonts.get("heading"), size, bold=True, text=text)
        lines = _wrap_text(draw, text, font, max_width)
        if (len(lines) <= 6 and len(lines) * int(size * 1.25) <= CARD_SIZE[1] * 0.7) or size <= 36:
            break
        size -= 4
    if len(lines) > 6:
        words = text.split()
        while words and len(lines) > 6:
            words.pop()
            lines = _wrap_text(draw, " ".join(words) + "…", font, max_width)

    line_height = int(size * 1.25)
    total_height = len(lines) * line_height
    y = (CARD_SIZE[1] - total_height) // 2

    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        line_width = box[2] - box[0]
        x = (CARD_SIZE[0] - line_width) // 2
        draw.text((x, y), line, font=font, fill=fg)
        y += line_height

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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


async def pick_default_image(
    *, piece_content: str, brand_profile: dict, workspace_id: str, user_id: str,
) -> Optional[MediaAsset]:
    """The single entry point run_text_pipeline calls right after a piece's
    text is final. Never raises."""
    visual_identity = brand_profile.get("visual_identity") or {}
    reference_ids = visual_identity.get("reference_media_ids") or []

    try:
        brand_asset = await _pick_brand_asset(reference_ids, workspace_id)
        if brand_asset:
            return brand_asset
    except Exception as exc:  # noqa: BLE001
        logger.error("Brand-asset match failed for workspace %s: %s", workspace_id, exc)

    try:
        topic = _scene_topic(piece_content)
        ai_asset = await generate_brand_image(
            topic=topic, brand_profile=brand_profile,
            workspace_id=workspace_id, user_id=user_id,
        )
        if ai_asset:
            return ai_asset
    except Exception as exc:  # noqa: BLE001
        logger.error("AI image generation failed for workspace %s: %s", workspace_id, exc)

    try:
        hook_text = _hook_line(piece_content)
        png_bytes = render_quote_card(hook_text, visual_identity)
        url = await upload_file(png_bytes, UploadContentType.IMAGE, user_id)
        asset = MediaAsset(
            id=str(uuid4()),
            workspace_id=workspace_id,
            kind=MediaKind.IMAGE,
            url=url,
            mime_type="image/png",
            width=CARD_SIZE[0],
            height=CARD_SIZE[1],
            source=MediaSource.GENERATED_TEMPLATE,
            created_by=user_id,
            created_at=datetime.now(timezone.utc),
            qa_flagged=True,
            qa_flag_reason=f"No AI picture, so this is a text card. {last_failure_reason() or 'The picture could not be made.'}",
        )
        await media_assets.insert_one(asset.model_dump())
        return asset
    except Exception as exc:  # noqa: BLE001
        logger.error("Quote-card render failed for workspace %s: %s", workspace_id, exc)
        return None
