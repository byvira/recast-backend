"""Trust score — shadow mode only. Nothing here ever publishes.

A score (0–100) per ``(workspace, pipeline, platform)`` saying how much the
workspace's own people already trust that kind of draft, from the last 60 days:

* ``untouched``  — share of approved drafts approved with no edits (v1)   · 35%
* ``kept``       — share of decided drafts not rejected                   · 25%
* ``quality``    — share of drafts that passed the quality gate           · 20%
* ``delivered``  — share of publish attempts that went live               · 20%
  (dropped and the rest re-weighted when nothing has been published yet)

No score below ``MIN_DECIDED`` decided drafts — too little to mean anything.

**Shadow record:** whenever a person approves or rejects a draft, we log
whether the score *would* have auto-published it next to what the person
actually did. Agreement over time is the evidence for (or against) ever
turning auto-publish on. When a tuple has ``RECOMMEND_MIN_DECISIONS`` shadow
decisions at ``RECOMMEND_MIN_AGREEMENT`` agreement, Odette recommends it to
admins — the switch itself stays a human decision.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import (
    autonomy_shadow,
    autonomy_trust,
    content_pieces,
    workspace_insights,
    workspaces,
)
from app.shared.activity import project_odette_insight

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=60)
MIN_DECIDED = 10
#: Provisional default — tune once shadow agreement data exists. Admins can
#: override per platform (PUT /api/v1/activity/autonomy/threshold).
PROVISIONAL_THRESHOLD = 80
SCORE_MAX_AGE = timedelta(hours=26)
RECOMMEND_MIN_DECISIONS = 20
RECOMMEND_MIN_AGREEMENT = 0.9
RECOMMEND_REPEAT_WINDOW = timedelta(days=30)

_WEIGHTS = {"untouched": 0.35, "kept": 0.25, "quality": 0.20, "delivered": 0.20}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(workspace_id: str, pipeline_type: str, platform: str) -> str:
    return f"{workspace_id}:{pipeline_type}:{platform}"


async def compute_trust(workspace_id: str, platform: str, pipeline_type: str = "text") -> dict:
    """One aggregation over the window's drafts for this tuple."""
    match = {
        "workspace_id": workspace_id,
        "platform": platform,
        "deleted": {"$ne": True},
        "created_at": {"$gte": _now() - WINDOW},
    }
    if pipeline_type != "text":
        match["pipeline_type"] = pipeline_type
    rows = await content_pieces.aggregate([
        {"$match": match},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "approved": {"$sum": {"$cond": [{"$eq": ["$approval_status", "approved"]}, 1, 0]}},
            "approved_v1": {"$sum": {"$cond": [{"$and": [
                {"$eq": ["$approval_status", "approved"]},
                {"$lte": [{"$ifNull": ["$version_count", 1]}, 1]},
            ]}, 1, 0]}},
            "rejected": {"$sum": {"$cond": [{"$eq": ["$approval_status", "rejected"]}, 1, 0]}},
            "quality": {"$sum": {"$cond": [{"$eq": ["$quality_passed", True]}, 1, 0]}},
            "published": {"$sum": {"$cond": [{"$eq": ["$publish_status", "published"]}, 1, 0]}},
            "failed": {"$sum": {"$cond": [{"$eq": ["$publish_status", "failed"]}, 1, 0]}},
        }},
    ]).to_list(length=1)
    c = rows[0] if rows else {}
    decided = c.get("approved", 0) + c.get("rejected", 0)
    components: dict[str, float] = {}
    if c.get("approved"):
        components["untouched"] = c["approved_v1"] / c["approved"]
    if decided:
        components["kept"] = 1 - c["rejected"] / decided
    if c.get("total"):
        components["quality"] = c["quality"] / c["total"]
    attempted = c.get("published", 0) + c.get("failed", 0)
    if attempted:
        components["delivered"] = c["published"] / attempted

    score: Optional[int] = None
    if decided >= MIN_DECIDED and components:
        weight = sum(_WEIGHTS[k] for k in components)
        score = round(100 * sum(_WEIGHTS[k] * v for k, v in components.items()) / weight)
    return {
        "score": score,
        "components": {k: round(v, 3) for k, v in components.items()},
        "decided": decided,
        "drafts": c.get("total", 0),
    }


async def _threshold(workspace_id: str, platform: str) -> int:
    ws = await workspaces.find_one({"id": workspace_id}, {"autonomy": 1}) or {}
    return int(((ws.get("autonomy") or {}).get("thresholds") or {}).get(platform, PROVISIONAL_THRESHOLD))


async def refresh_tuple(workspace_id: str, platform: str, pipeline_type: str = "text") -> dict:
    trust = await compute_trust(workspace_id, platform, pipeline_type)
    threshold = await _threshold(workspace_id, platform)
    await autonomy_trust.update_one(
        {"_id": _key(workspace_id, pipeline_type, platform)},
        {
            "$set": {
                "workspace_id": workspace_id, "pipeline_type": pipeline_type, "platform": platform,
                **trust, "threshold": threshold,
                "eligible": trust["score"] is not None and trust["score"] >= threshold,
                "computed_at": _now(),
            },
            "$setOnInsert": {"shadow": {"total": 0, "agree": 0}},
        },
        upsert=True,
    )
    return {**trust, "threshold": threshold}


async def record_shadow(piece: dict, new_status: str) -> None:
    """A person just approved or rejected ``piece`` (its state *before* the
    change). Log what the trust score would have done. Never raises."""
    try:
        if new_status not in ("approved", "rejected") or piece.get("approval_status") == new_status:
            return
        workspace_id, platform = piece.get("workspace_id"), piece.get("platform")
        if not workspace_id or not platform:
            return
        pipeline_type = piece.get("pipeline_type", "text")
        key = _key(workspace_id, pipeline_type, platform)
        doc = await autonomy_trust.find_one({"_id": key})
        computed = doc.get("computed_at") if doc else None
        if computed and computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
        if not doc or not computed or _now() - computed > SCORE_MAX_AGE:
            doc = await refresh_tuple(workspace_id, platform, pipeline_type)
        score, threshold = doc.get("score"), doc.get("threshold", PROVISIONAL_THRESHOLD)
        if score is None:
            return   # not enough history for the score to mean anything yet

        if new_status == "rejected":
            action = "rejected"
        elif int(piece.get("version_count") or 1) <= 1:
            action = "approved_unedited"
        else:
            action = "approved_after_edit"
        would_auto = score >= threshold
        agree = would_auto == (action == "approved_unedited")

        await autonomy_shadow.insert_one({
            "_id": str(uuid4()),
            "workspace_id": workspace_id, "pipeline_type": pipeline_type, "platform": platform,
            "piece_id": piece.get("piece_id"), "score": score, "threshold": threshold,
            "would_auto_publish": would_auto, "human_action": action, "agree": agree,
            "recorded_at": _now(),
        })
        await autonomy_trust.update_one(
            {"_id": key},
            {"$inc": {"shadow.total": 1, "shadow.agree": 1 if agree else 0}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("trust shadow record failed for %s: %s", piece.get("piece_id"), exc)


async def _maybe_recommend(tuple_doc: dict) -> Optional[str]:
    shadow = tuple_doc.get("shadow") or {}
    total, agree = shadow.get("total", 0), shadow.get("agree", 0)
    if total < RECOMMEND_MIN_DECISIONS or agree / total < RECOMMEND_MIN_AGREEMENT or not tuple_doc.get("eligible"):
        return None
    ws_id, platform = tuple_doc["workspace_id"], tuple_doc["platform"]
    if await workspace_insights.find_one({
        "workspace_id": ws_id, "evidence.metrics.source": "trust_shadow",
        "evidence.metrics.platform": platform,
        "created_at": {"$gte": _now() - RECOMMEND_REPEAT_WINDOW},
    }):
        return None
    now = _now()
    rate = round(100 * agree / total)
    doc = {
        "_id": str(uuid4()),
        "workspace_id": ws_id,
        "kind": "recommendation",
        "title": f"{platform} drafts look ready for auto-publish",
        "body_persona": (
            f"Over the last {total} {platform} drafts your team decided on, the trust score "
            f"({tuple_doc['score']}/100) called it the same way you did {rate}% of the time. "
            f"Auto-publish stays off until an owner turns it on — this is the evidence to decide with."
        ),
        "rationale": f"Shadow agreement {agree}/{total}; score {tuple_doc['score']} ≥ threshold {tuple_doc['threshold']}.",
        "evidence": {"event_ids": [], "signal_ids": [], "metrics": {
            "source": "trust_shadow", "platform": platform, "score": tuple_doc["score"],
            "agreement": round(agree / total, 3), "decisions": total,
        }},
        "priority": "medium",
        "pipeline_scope": [tuple_doc.get("pipeline_type", "text")],
        "langsmith_run_url": "",
        "status": "new",
        "created_at": now,
        "updated_at": now,
        "created_by_agent_run": f"trust:{ws_id}:{now.isoformat()}",
    }
    await workspace_insights.insert_one(doc)
    await project_odette_insight(doc)
    return doc["_id"]


@distributed_job_lock("autonomy_trust_refresh", ttl_seconds=1800)
async def autonomy_trust_refresh(ctx: Optional[dict] = None) -> dict:
    """Daily: recompute every active tuple's score, then check whether any has
    earned a recommendation."""
    since = _now() - WINDOW
    tuples = await content_pieces.aggregate([
        {"$match": {"created_at": {"$gte": since}, "deleted": {"$ne": True}}},
        {"$group": {"_id": {"ws": "$workspace_id", "platform": "$platform",
                            "pt": {"$ifNull": ["$pipeline_type", "text"]}}}},
    ]).to_list(length=10_000)
    refreshed = recommended = 0
    for t in tuples:
        ws_id, platform, pt = t["_id"].get("ws"), t["_id"].get("platform"), t["_id"].get("pt")
        if not ws_id or not platform:
            continue
        try:
            await refresh_tuple(ws_id, platform, pt)
            refreshed += 1
            doc = await autonomy_trust.find_one({"_id": _key(ws_id, pt, platform)})
            if doc and await _maybe_recommend(doc):
                recommended += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("trust refresh failed for %s/%s: %s", ws_id, platform, exc)
    return {"refreshed": refreshed, "recommended": recommended}
