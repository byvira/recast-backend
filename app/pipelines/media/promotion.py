"""The Audio Promotion Bundle: from the best moments of a recording, draft posts and quote pictures to promote it.

What it does, in order: finds the quotable moments (the same pick the "suggest clips" button makes), writes one draft post from each
moment for the chosen platform, and (when asked) makes one quote picture from each moment. Everything is a draft for review. Nothing
is published or scheduled, and the member sees the whole price before anything is charged.

This file only works out the plan and its price. The work itself is done by the existing text and picture pipelines.
"""

from __future__ import annotations

from typing import Any

from app.models.text import Platform

MAX_MOMENTS = 5
#: A quote this long or longer is not used as a picture headline (the picture would be unreadable); the post still uses it.
PICTURE_HEADLINE_MAX = 140


async def estimate(platform: str, count: int, with_pictures: bool) -> dict[str, Any]:
    """The price of the bundle in credits, worked out from the current prices. Nothing is made or charged here."""
    from app.billing import costs
    from app.pipelines.text.orchestrator import _post_action

    count = max(1, min(int(count), MAX_MOMENTS))
    table = await costs.costs()
    try:
        post_action = _post_action(Platform(platform))
    except ValueError:
        post_action = "text_post"
    finding = costs.price_with(table, "audio_script")
    posts = count * costs.price_with(table, post_action)
    pictures = count * costs.price_with(table, "picture") if with_pictures else 0
    return {
        "moments": count, "platform": platform, "with_pictures": bool(with_pictures),
        "finding_credits": finding, "post_credits": posts, "picture_credits": pictures,
        "credits": finding + posts + pictures,
    }


async def blockers(workspace_id: str, credits_needed: int) -> tuple[list[str], int | None]:
    """(reasons it cannot start, credits the workspace can spend or None when there is no limit)."""
    from app.billing import credits

    reasons: list[str] = []
    pool = await credits.pool_for(workspace_id)
    summary = await credits.summary(workspace_id)
    if pool.limited:
        reasons.append(credits.LIMITED_MESSAGE)
    spendable = None if summary.get("left") is None else int(summary["left"]) + int(summary.get("bought_left") or 0)
    if pool.enforced and spendable is not None and spendable < credits_needed:
        reasons.append(f"This needs about {credits_needed} credits and you have {spendable}. Buy a pack or wait for {summary.get('resets_on')}.")
    return reasons, spendable


def picture_headline(quote: str) -> str | None:
    """The quote as a picture headline, or None when it is too long to read on a picture."""
    text = " ".join((quote or "").split())
    return text if text and len(text) <= PICTURE_HEADLINE_MAX else None
