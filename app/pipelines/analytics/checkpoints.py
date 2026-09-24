"""Post-performance checkpoints — each post's metrics at fixed ages.

``post_metrics`` holds only the latest numbers per post (overwritten every
refresh), so it can't answer "how did this post do after 24 hours compared
with that one?". This module records the same metrics at four ages —
1h, 24h, 72h and 7d after publish — into ``post_metric_checkpoints``. That
fixed-age comparison is what the feedback loop (Remy's coaching, Odette's
workspace read, the trust score) learns from.

A checkpoint is only recorded inside its window (see ``CHECKPOINTS``). If the
job wasn't running when a window was open, that checkpoint is recorded as
``missed`` rather than filled with numbers taken at the wrong age.
After 7d a post is never polled here again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import content_pieces, post_metric_checkpoints, workspace_connections
from app.pipelines.analytics.aggregator import fetch_post_metrics_all

logger = logging.getLogger(__name__)

#: (label, target age, latest age it still counts as that checkpoint).
CHECKPOINTS: list[tuple[str, timedelta, timedelta]] = [
    ("1h", timedelta(hours=1), timedelta(hours=3)),
    ("24h", timedelta(hours=24), timedelta(hours=30)),
    ("72h", timedelta(hours=72), timedelta(hours=84)),
    ("7d", timedelta(days=7), timedelta(days=8)),
]
LAST_WINDOW = CHECKPOINTS[-1][2]

#: Cap per run so one busy workspace can't monopolise a tick; the rest are
#: picked up on the next one (runs every 15 minutes).
MAX_POSTS_PER_WORKSPACE = 100


def _as_utc(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def published_time(piece: dict) -> Optional[datetime]:
    """``published_at`` where recorded; older pieces fall back to
    ``updated_at`` — the same convention the analytics calendar uses."""
    return _as_utc(piece.get("published_at")) or _as_utc(piece.get("updated_at"))


def due_checkpoints(age: timedelta, done: set[str]) -> tuple[Optional[str], list[str]]:
    """Which checkpoint to capture now (at most one — a single fetch can only
    honestly represent one age) and which past windows were missed."""
    capture: Optional[str] = None
    missed: list[str] = []
    for label, target, latest in CHECKPOINTS:
        if label in done or age < target:
            continue
        if age <= latest:
            capture = label
        else:
            missed.append(label)
    return capture, missed


@distributed_job_lock("capture_metric_checkpoints", ttl_seconds=900)
async def capture_metric_checkpoints(ctx: Optional[dict] = None) -> dict:
    """Scheduled every 15 minutes (app.workers.jobs)."""
    now = datetime.now(timezone.utc)
    workspace_ids = await workspace_connections.distinct("workspace_id", {"is_active": True})
    captured = missed = 0
    for workspace_id in workspace_ids:
        try:
            c, m = await _capture_workspace(workspace_id, now)
            captured += c
            missed += m
        except Exception as exc:  # noqa: BLE001
            logger.error("checkpoint capture failed for workspace %s: %s", workspace_id, exc)
    if captured or missed:
        logger.info("metric checkpoints: captured=%d missed=%d", captured, missed)
    return {"captured": captured, "missed": missed}


async def _capture_workspace(workspace_id: str, now: datetime) -> tuple[int, int]:
    earliest = now - LAST_WINDOW
    pieces = await content_pieces.find(
        {
            "workspace_id": workspace_id,
            "publish_status": "published",
            "platform_post_id": {"$nin": [None, ""]},
            "checkpoints_complete": {"$ne": True},
            "$or": [
                {"published_at": {"$gte": earliest}},
                {"published_at": {"$exists": False}, "updated_at": {"$gte": earliest}},
            ],
        },
        {
            "piece_id": 1, "platform": 1, "publish_target": 1, "platform_post_id": 1,
            "published_at": 1, "updated_at": 1, "metric_checkpoints": 1,
            "user_id": 1, "brand_id": 1, "word_count": 1, "pipeline_type": 1,
        },
    ).limit(MAX_POSTS_PER_WORKSPACE).to_list(length=MAX_POSTS_PER_WORKSPACE)

    to_fetch: list[tuple[dict, str, list[str], timedelta]] = []
    missed_total = 0
    for piece in pieces:
        published = published_time(piece)
        if not published:
            continue
        age = now - published
        done = set((piece.get("metric_checkpoints") or {}).keys())
        capture, missed = due_checkpoints(age, done)
        if missed:
            missed_total += len(missed)
            await content_pieces.update_one(
                {"piece_id": piece["piece_id"]},
                {"$set": {f"metric_checkpoints.{label}": "missed" for label in missed}},
            )
        if capture:
            to_fetch.append((piece, capture, missed, age))
        elif age > LAST_WINDOW:
            await content_pieces.update_one(
                {"piece_id": piece["piece_id"]}, {"$set": {"checkpoints_complete": True}}
            )

    if not to_fetch:
        return 0, missed_total

    posts = [
        {
            "piece_id": piece["piece_id"],
            "platform": (piece.get("publish_target") or piece.get("platform") or "").lower(),
            "platform_post_id": piece["platform_post_id"],
            "platform_user_id": "",
        }
        for piece, *_ in to_fetch
    ]
    metrics_by_piece = {
        m.post_id: m for m in await fetch_post_metrics_all(workspace_id=workspace_id, posts=posts)
    }

    captured = 0
    for piece, label, _missed, age in to_fetch:
        m = metrics_by_piece.get(piece["piece_id"])
        if not m:
            continue   # platform fetch failed — retried next tick while the window is open
        await post_metric_checkpoints.update_one(
            {"_id": f"{piece['piece_id']}:{label}"},
            {"$set": {
                "workspace_id": workspace_id,
                "piece_id": piece["piece_id"],
                "checkpoint": label,
                "age_hours": round(age.total_seconds() / 3600, 2),
                "platform": m.platform,
                "platform_post_id": m.platform_post_id,
                "user_id": piece.get("user_id", ""),
                "brand_id": piece.get("brand_id", ""),
                "pipeline_type": piece.get("pipeline_type", "text"),
                "word_count": piece.get("word_count", 0),
                "metrics": {
                    "likes": m.likes, "comments": m.comments, "shares": m.shares,
                    "reposts": m.reposts, "saves": m.saves, "clicks": m.clicks,
                    "impressions": m.impressions, "reach": m.reach, "views": m.views,
                    "engagement_rate": m.engagement_rate,
                },
                "published_at": published_time(piece),
                "captured_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        update: dict = {f"metric_checkpoints.{label}": "captured"}
        if label == CHECKPOINTS[-1][0]:
            update["checkpoints_complete"] = True
        await content_pieces.update_one({"piece_id": piece["piece_id"]}, {"$set": update})
        captured += 1
    return captured, missed_total
