"""Packs: several images from one request.

The background generator does not use a seed, so asking it for the same prompt several times
can return the same picture. Each image after the first gets its own short direction, so a
pack is a set of different takes on the same idea, not copies. Pure, no network.
"""

from __future__ import annotations

from app.prompts.registry import load_fixture, load_prompt

MAX_PACK_SIZE = 10
PACK_SIZE_CHOICES = (1, 3, 4, 5)

# Different directions, cycled; kept plain so they read as art direction to an image model.
_VARIATIONS = tuple(load_fixture("image_pack_variations"))


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
        prompts.append(load_prompt("media/image_pack_variation", base=base, number=i + 1, total=n, direction=direction))
    return prompts
