"""Reading the look of a brand's reference pictures, so new pictures can follow it.

A brand can keep up to a few reference pictures. They were only used to pick a ready picture that already matches. Here they also become a
style guide: their colours, how dark or bright they are, how muted or vivid, warm or cool, and how much empty space they leave are read
from the pictures themselves (no model call, no cost) and written as one plain sentence that is added to what the picture model is told.

The reading is kept per brand and reused until the set of reference pictures changes."""
from __future__ import annotations

import colorsys
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

from PIL import Image, ImageFilter, ImageStat

logger = logging.getLogger(__name__)

MAX_REFERENCES = 3
_SIZE = 64


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def read_one(data: bytes) -> Optional[dict[str, Any]]:
    """What one picture looks like, as numbers: average brightness and colour strength (0 to 1), the share of its colourful pixels that are
    warm and cool, how much detail it has, and its main colours."""
    try:
        with Image.open(BytesIO(data)) as source:
            img = source.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    img.thumbnail((_SIZE, _SIZE))
    pixels = list(img.getdata())
    if not pixels:
        return None
    value = saturation = 0.0
    warm = cool = coloured = 0
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        value += v
        saturation += s
        if s > 0.18 and v > 0.15:
            coloured += 1
            degrees = h * 360
            if degrees <= 70 or degrees >= 310:
                warm += 1
            elif 170 <= degrees <= 270:
                cool += 1
    count = len(pixels)
    detail = ImageStat.Stat(img.convert("L").filter(ImageFilter.FIND_EDGES)).mean[0] / 255
    palette = img.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
    raw = palette.getpalette() or []
    counts = sorted(palette.getcolors() or [], reverse=True)
    main = [_hex((raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2])) for _, i in counts[:3] if i * 3 + 2 < len(raw)]
    return {
        "brightness": value / count, "colour": saturation / count, "detail": detail,
        "warm": warm / coloured if coloured else 0.0, "cool": cool / coloured if coloured else 0.0, "main": main,
    }


def describe(reads: list[dict[str, Any]]) -> dict[str, Any]:
    """The words for what several pictures have in common, and the colours they share. Empty when there is nothing to read."""
    if not reads:
        return {"line": "", "traits": [], "colours": []}
    n = len(reads)
    brightness = sum(r["brightness"] for r in reads) / n
    colour = sum(r["colour"] for r in reads) / n
    detail = sum(r["detail"] for r in reads) / n
    warm = sum(r["warm"] for r in reads) / n
    cool = sum(r["cool"] for r in reads) / n
    traits: list[str] = []
    traits.append("dark tones" if brightness < 0.33 else "light, bright tones" if brightness > 0.67 else "mid tones")
    traits.append("muted colours" if colour < 0.27 else "vivid colours" if colour > 0.55 else "balanced colours")
    if warm - cool > 0.25:
        traits.append("warm light")
    elif cool - warm > 0.25:
        traits.append("cool light")
    traits.append("plenty of empty space" if detail < 0.04 else "a busy, detailed look" if detail > 0.11 else "a balanced amount of detail")
    colours: list[str] = []
    for r in reads:
        for c in r["main"]:
            if c not in colours:
                colours.append(c)
    colours = colours[:3]
    line = "Follow the look of the brand's own pictures: " + ", ".join(traits) + "."
    if colours:
        line += " The main colours are close to " + ", ".join(colours) + "."
    return {"line": line, "traits": traits, "colours": colours}


async def _picture_bytes(media_id: str, workspace_id: str) -> Optional[bytes]:
    from app.db.mongo import media_assets
    from app.pipelines.media.image_generation import _fetch_logo

    doc = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id, "kind": "image"}, {"url": 1})
    return await _fetch_logo((doc or {}).get("url") or "") if doc else None


async def style_of(brand: dict, workspace_id: str) -> dict[str, Any]:
    """The brand's reference-picture style: {line, traits, colours}. Read once and kept until the reference pictures change; empty when the
    brand has none or none can be read. Never raises, because a picture must not fail over its style hint."""
    from app.db.mongo import get_db

    ids = [str(i) for i in ((brand.get("visual_identity") or {}).get("reference_media_ids") or [])][:MAX_REFERENCES]
    empty: dict[str, Any] = {"line": "", "traits": [], "colours": []}
    if not ids or not brand.get("id"):
        return empty
    key = ",".join(ids)
    col = get_db()["brand_reference_style"]
    try:
        kept = await col.find_one({"brand_id": brand["id"], "workspace_id": workspace_id}, {"_id": 0})
        if kept and kept.get("key") == key:
            return {"line": kept.get("line", ""), "traits": kept.get("traits", []), "colours": kept.get("colours", [])}
        reads: list[dict[str, Any]] = []
        for media_id in ids:
            data = await _picture_bytes(media_id, workspace_id)
            read = read_one(data) if data else None
            if read:
                reads.append(read)
        made = describe(reads)
        await col.update_one(
            {"brand_id": brand["id"], "workspace_id": workspace_id},
            {"$set": {**made, "key": key, "at": datetime.now(timezone.utc)}}, upsert=True,
        )
        return made
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the reference pictures for brand %s: %s", brand.get("id"), exc)
        return empty


async def with_style_line(prompt: str, brand: dict, workspace_id: str) -> str:
    """`prompt` with the brand's reference-picture style added, or as it was when the brand has none."""
    made = await style_of(brand, workspace_id)
    line = made.get("line") or ""
    out = f"{prompt.rstrip()} {line}" if line and line not in prompt else prompt
    from app.pipelines.media import request_refs

    sent = request_refs.active_line()                       # the pictures the person sent with this request, when there are any
    return f"{out.rstrip()} {sent}" if sent and sent not in out else out
