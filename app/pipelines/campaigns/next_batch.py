"""What "make the next batch" will do for a campaign, worked out before anything is made or charged.

A batch uses the campaign's own settings (topic, platforms, days, picture and audio plans, tone, language), so the preview is plain
reading of those settings plus the credit prices. Nothing is created here. The batch itself is started by the existing
`POST /campaigns/{id}/runs`; it makes drafts for review and never publishes by itself, and the member schedules the drafts afterwards
(the Publish Workspace suggests the best times).

Credits: planning costs a fixed amount, every post is charged as it is made (one price for most platforms, a higher one for long form),
and every picture is charged as it is made. Recordings and videos are charged by length, which is not known before they are made, so
they are counted but not priced."""
from __future__ import annotations

from typing import Any

from app.models.text import Platform
from app.pipelines.campaigns import media as campaign_media

MAX_DAYS = 30


def _days(campaign: dict[str, Any]) -> int:
    return max(1, min(MAX_DAYS, int((campaign.get("cadence") or {}).get("days_per_batch") or 7)))


def platforms_by_day(campaign: dict[str, Any]) -> list[list[str]]:
    """The platforms written for on each day of a batch: the per-day list when the campaign has one, else the same list every day."""
    days = _days(campaign)
    per_day = campaign.get("platforms_by_day")
    if per_day:
        return [list(per_day[i]) if i < len(per_day) else list(campaign.get("platforms") or []) for i in range(days)]
    return [list(campaign.get("platforms") or []) for _ in range(days)]


async def connection_warnings(workspace_id: str, platforms: list[str]) -> list[str]:
    """One sentence for each platform the batch is for that is not connected yet. Drafts can still be made; publishing them needs the
    account, so the member is told before they start. A platform that is not published through a connected account is left out."""
    from app.pipelines.publish.spine import platform_key
    from app.pipelines.publish.token_store import get_token
    from app.platforms.base import import_all, resolve_platform_by_display_value

    out: list[str] = []
    try:
        import_all()
        for name in platforms:
            definition = resolve_platform_by_display_value(name)
            if not definition or not definition.publisher_cls:
                continue
            if not await get_token(workspace_id, platform_key(name)):
                out.append(f"{definition.label} is not connected, so its posts can be made but not published until you connect it.")
    except Exception:  # noqa: BLE001 - a hint, never a reason to stop the screen
        return out
    return out


async def preview(campaign: dict[str, Any]) -> dict[str, Any]:
    """The batch in numbers: how many days and posts, what each part costs in credits, and how many pictures, recordings and videos."""
    from app.billing import costs
    from app.pipelines.text.orchestrator import _post_action

    table = await costs.costs()
    per_day = platforms_by_day(campaign)
    posts = sum(len(day) for day in per_day)
    writing = 0
    for day in per_day:
        for name in day:
            try:
                writing += costs.price_with(table, _post_action(Platform(name)))
            except ValueError:
                writing += costs.price_with(table, "text_post")
    planning = costs.price_with(table, "campaign_plan")
    files = campaign_media.estimate(campaign.get("media_plan"), posts)
    pictures = files["images"] * costs.price_with(table, "picture")
    return {
        "campaign_id": campaign["id"],
        "name": campaign.get("name") or "Untitled",
        "topic": campaign.get("topic_cluster") or "",
        "platforms": sorted({p for day in per_day for p in day}),
        "days": len(per_day),
        "posts": posts,
        "images": files["images"], "audio": files["audio"], "videos": files["videos"],
        "planning_credits": planning,
        "writing_credits": writing,
        "picture_credits": pictures,
        # what is known before it starts; recordings and videos are charged by length on top of this
        "credits": planning + writing + pictures,
        "media_by_length": bool(files["audio"] or files["videos"]),
    }
