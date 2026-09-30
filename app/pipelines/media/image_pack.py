"""Packs: several images from one request.

The background generator does not use a seed, so asking it for the same prompt several times
can return the same picture. Each image after the first gets its own short direction, so a
pack is a set of different takes on the same idea, not copies. Pure, no network.
"""

from __future__ import annotations

MAX_PACK_SIZE = 10
PACK_SIZE_CHOICES = (1, 3, 4, 5)

# Different directions, cycled; kept plain so they read as art direction to an image model.
_VARIATIONS = (
    "a wide establishing composition",
    "a close-up detail shot",
    "a different angle with a new colour accent",
    "a minimal composition with generous empty space",
    "a bold, high-contrast composition",
    "a softer, atmospheric composition",
    "a top-down composition",
    "a symmetrical, centred composition",
    "a layered composition with depth",
)


def clamp_pack_size(count: int | None) -> int:
    if count is None:
        return 1
    return max(1, min(MAX_PACK_SIZE, int(count)))


def pack_prompts(prompt: str, count: int | None) -> list[str]:
    """One prompt per image. The first is exactly what the member wrote."""
    n = clamp_pack_size(count)
    base = (prompt or "").strip()
    prompts = [base]
    for i in range(1, n):
        direction = _VARIATIONS[(i - 1) % len(_VARIATIONS)]
        prompts.append(f"{base}. Variation {i + 1} of {n}: {direction}.")
    return prompts
