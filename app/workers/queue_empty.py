"""Tells a workspace when nothing is scheduled any more, and offers to make the next batch with the same settings.

Each morning the workspaces that have been publishing on a schedule (posts they scheduled or a campaign made) are looked at. When the last
of it has gone out and nothing is upcoming, the owner and the person who made the campaign each get one note in their Activity (and a
device notice, following their own notification choices): "Your queue is empty", with the way to make the next batch. The batch itself is
the existing campaign batch, started by the member from a confirm screen that shows what it will make and what it costs.

Rules, so it stays useful and never nags:
  * Once for each empty period. The period is named by the time of the last post that went out, so nothing more is sent until something
    new has been scheduled and has gone out too.
  * Not when something is upcoming: a scheduled post, a planned post waiting its time, or a campaign that makes its own batches.
  * Only for a workspace that published on a schedule in the last 30 days, so a workspace that is simply not used is left alone.
  * Not when the owner turned the notice off for the workspace, or a campaign was muted ("don't remind me").
  * Not while the plan is on hold or the credits are used up: there is nothing the member could do with the offer.
It counts what is already recorded. No model is called and nothing is charged."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_campaigns_collection, get_db
from app.shared.activity import record_system
from app.shared.activity.i18n import phrase

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
#: The last post must have been out at least this long, so a notice never lands the moment the final post goes out.
QUIET_FOR = timedelta(hours=1)
MAX_WORKSPACES = 3000


def _utc(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def last_scheduled_post_at(workspace_id: str, since: datetime) -> Optional[datetime]:
    """When the latest post that went out on a schedule (or from a campaign) went out, within the look-back; None when there was none."""
    row = await get_db()["content_pieces"].find_one(
        {
            "workspace_id": workspace_id, "deleted": {"$ne": True}, "publish_status": "published", "published_at": {"$gte": since},
            "$or": [{"publish_scheduled_at": {"$nin": [None, ""]}}, {"campaign_id": {"$nin": [None, ""]}}],
        },
        {"published_at": 1}, sort=[("published_at", -1)],
    )
    return _utc((row or {}).get("published_at"))


async def has_upcoming(workspace_id: str, now: datetime) -> bool:
    """Whether anything is still on its way out: a queued post, a planned post waiting for its time, or a campaign that makes its own batches."""
    db = get_db()
    if await db["content_pieces"].count_documents({"workspace_id": workspace_id, "deleted": {"$ne": True}, "publish_status": {"$in": ["queued", "publishing"]}}, limit=1):
        return True
    planned = {
        "workspace_id": workspace_id, "deleted": {"$ne": True}, "approval_status": {"$ne": "rejected"},
        "publish_status": {"$in": [None, "pending"]},
        "$or": [{"intended_publish_at": {"$gt": now}}, {"intended_publish_at": {"$gt": now.isoformat()}}],
    }
    if await db["content_pieces"].count_documents(planned, limit=1):
        return True
    automatic = {"workspace_id": workspace_id, "deleted": {"$ne": True}, "status": "active", "cadence.frequency": {"$in": ["daily", "weekly"]}}
    return bool(await get_campaigns_collection().count_documents(automatic, limit=1))


async def next_campaign(workspace_id: str) -> Optional[dict[str, Any]]:
    """The campaign the offer is for: the one that made posts most recently. A muted campaign, a draft and a repurpose are never offered."""
    cursor = get_campaigns_collection().find(
        {"workspace_id": workspace_id, "deleted": {"$ne": True}, "kind": {"$ne": "repurpose"}, "status": {"$in": ["active", "paused", "completed"]}},
    ).sort("last_generated_at", -1).limit(1)
    rows = await cursor.to_list(length=1)
    if not rows or rows[0].get("remind_when_empty") is False:
        return None
    return rows[0]


async def _spare_credits(workspace_id: str) -> bool:
    """False when the plan is on hold or the credits are used up, so there is nothing the member could do with the offer."""
    from app.billing import credits

    try:
        pool = await credits.pool_for(workspace_id)
        if pool.limited:
            return False
        if not pool.enforced:
            return True
        summary = await credits.summary(workspace_id)
        left = summary.get("left")
        return left is None or int(left) + int(summary.get("bought_left") or 0) > 0
    except Exception as exc:  # noqa: BLE001 - when the credits cannot be read the offer is still fine to make
        logger.warning("Could not read the credits of %s for the empty-queue notice: %s", workspace_id, exc)
        return True


async def check_workspace(workspace_id: str, now: datetime) -> str:
    """One workspace. Returns "notified" or "skipped: <why>", so a test (and the logs) can say what happened."""
    workspace = await get_db()["workspaces"].find_one({"id": workspace_id, "deleted_at": {"$exists": False}}, {"owner_id": 1, "queue_empty_notice": 1, "name": 1})
    if not workspace:
        return "skipped: no workspace"
    if workspace.get("queue_empty_notice") is False:
        return "skipped: switched off"
    last = await last_scheduled_post_at(workspace_id, now - timedelta(days=LOOKBACK_DAYS))
    if last is None:
        return "skipped: nothing published on a schedule lately"
    if now - last < QUIET_FOR:
        return "skipped: the last post only just went out"
    if await has_upcoming(workspace_id, now):
        return "skipped: something is upcoming"
    if not await _spare_credits(workspace_id):
        return "skipped: plan on hold or no credits"
    campaign = await next_campaign(workspace_id)
    # a workspace whose only campaign was muted is left alone; one with no campaign at all still gets the plain "your schedule is empty"
    if campaign is None and await get_campaigns_collection().count_documents({"workspace_id": workspace_id, "deleted": {"$ne": True}, "remind_when_empty": False}, limit=1):
        return "skipped: muted"
    # one notice for each empty period, named by the post that ended it, claimed with one write so two servers send it once
    claim = await get_db()["queue_empty_notices"].update_one(
        {"workspace_id": workspace_id, "last_post_at": last}, {"$setOnInsert": {"at": now, "campaign_id": (campaign or {}).get("id")}}, upsert=True,
    )
    if claim.upserted_id is None:
        return "skipped: already told for this empty period"

    recipients = [workspace.get("owner_id")]
    if campaign and campaign.get("created_by"):
        recipients.append(campaign["created_by"])
    else:
        row = await get_db()["content_pieces"].find_one({"workspace_id": workspace_id, "publish_status": "published", "published_at": last}, {"user_id": 1})
        recipients.append((row or {}).get("user_id"))
    people = [p for p in dict.fromkeys(recipients) if p]
    name = (campaign or {}).get("name") or ""
    href = f"/dashboard/campaigns/{campaign['id']}?nextBatch=1" if campaign else "/dashboard/pipelines/new"
    title = phrase("Your queue is empty") if campaign else phrase("Nothing is scheduled any more")
    detail = (
        phrase("Everything scheduled has gone out. Make the next batch of \"{name}\" with the same settings? You see what it makes and costs first.", name=name)
        if campaign else phrase("Everything scheduled has gone out and nothing is planned. Start a campaign or schedule some posts to keep going.")
    )
    for person in people:
        await record_system(
            workspace_id=workspace_id, key=f"queue-empty:{workspace_id}:{last.isoformat()}:{person}", actor_name="Ekavo", category="queue_empty",
            title=title, description=detail, href=href, visibility="member", member_user_id=person, target_id=(campaign or {}).get("id"),
            target_type="campaign" if campaign else None, metadata={"event": "queue.empty", "campaign_id": (campaign or {}).get("id")},
        )
    return "notified"


@distributed_job_lock("queue_empty", ttl_seconds=1800)
async def notify_empty_queues() -> int:
    """Called once each morning. Returns how many workspaces were told."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    ids = await get_db()["content_pieces"].distinct(
        "workspace_id",
        {"deleted": {"$ne": True}, "publish_status": "published", "published_at": {"$gte": since}},
    )
    told = 0
    for workspace_id in ids[:MAX_WORKSPACES]:
        try:
            if workspace_id and await check_workspace(workspace_id, now) == "notified":
                told += 1
        except Exception as exc:  # noqa: BLE001 - one workspace's problem never stops the others
            logger.warning("Could not check the empty-queue notice for %s: %s", workspace_id, exc)
    return told
