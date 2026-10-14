"""Whether a video is the member's own footage, AI made, stock, or a mix: one answer carried from the library through the
project and the render to every screen that shows the video."""
from __future__ import annotations

from typing import Iterable

from app.models.media import (
    ORIGIN_AI, ORIGIN_BRAND, ORIGIN_MIXED, ORIGIN_OF_SOURCE, ORIGIN_OWN, ORIGIN_STOCK, ORIGIN_TEMPLATE, MediaSource,
)


def origin_of(doc: dict) -> str:
    """The origin of one library item (a stored row, which may predate the field)."""
    stored = doc.get("origin")
    if isinstance(stored, str) and stored:
        return stored
    try:
        return ORIGIN_OF_SOURCE.get(MediaSource(doc.get("source")), ORIGIN_OWN)
    except ValueError:
        return ORIGIN_OWN


def summarise(origins: Iterable[str]) -> dict:
    """The origin of a finished video from the origins of everything it used. A brand asset or a template counts as the
    member's own. `contains_ai` is true when any used item was made by AI; `contains_stock` when any came from stock."""
    seen = [o for o in origins]
    ai = [o for o in seen if o == ORIGIN_AI]
    stock = [o for o in seen if o == ORIGIN_STOCK]
    own = [o for o in seen if o in (ORIGIN_OWN, ORIGIN_BRAND, ORIGIN_TEMPLATE, ORIGIN_MIXED)]
    if not seen:
        kind = ORIGIN_OWN
    elif ai and not (own or stock):
        kind = ORIGIN_AI
    elif stock and not (own or ai):
        kind = ORIGIN_STOCK
    elif ai or stock:
        kind = ORIGIN_MIXED
    else:
        kind = ORIGIN_OWN
    return {"origin": kind, "contains_ai": bool(ai), "contains_stock": bool(stock)}


LABELS = {
    ORIGIN_OWN: "Your own", ORIGIN_AI: "AI generated", ORIGIN_STOCK: "Stock", ORIGIN_BRAND: "Brand asset",
    ORIGIN_TEMPLATE: "Template", ORIGIN_MIXED: "Includes AI or stock",
}
