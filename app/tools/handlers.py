"""Runners for the tools that have their own typed answer (see reads.py): who I am, credits, prices, plan limits, posts, campaigns, brands,
runs, what is waiting, what is scheduled and top assets.

Each one reads through the part of the app that already owns the data (credits, entitlements, the Library storage, the runs table), then
shapes the answer into the tool's typed output, which is checked before it leaves. Lists page with a cursor that names the last row seen
(its time and id), so a row added between two pages never repeats or hides another."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel

from app.billing import costs as credit_costs
from app.billing import credits
from app.db.mongo import get_db
from app.shared import entitlements, jobs, pipeline_runs
from app.tools import reads
from app.tools.executor import ToolFailure, handler
from app.tools.gatekeeper import Admission

_QUEUED_LIKE = ("queued", "publishing", "scheduled")


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value or "")


def _checked(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """The answer, held to the tool's own output type, so what leaves is exactly what the contract says."""
    try:
        return model.model_validate(data).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        raise ToolFailure("output_mismatch", "The answer did not match what this tool promises. This has been logged.", 502) from exc


def _encode(*parts: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts, default=str).encode("utf-8")).decode("ascii")


def _decode(cursor: Optional[str]) -> Optional[list[Any]]:
    if not cursor:
        return None
    try:
        parts = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception:  # noqa: BLE001
        raise ToolFailure("input_invalid", "That cursor is not valid. Start again without one.", 422)
    if not isinstance(parts, list) or len(parts) != 2:
        raise ToolFailure("input_invalid", "That cursor is not valid. Start again without one.", 422)
    return parts


def _args(admission: Admission) -> dict[str, Any]:
    return admission.arguments.model_dump(mode="json", exclude_none=True)


def _ws(admission: Admission) -> str:
    return admission.credential.workspace_id


# ── who and what ─────────────────────────────────────────────────────────

@handler("me.whoami")
async def whoami(admission: Admission) -> dict[str, Any]:
    ctx = await jobs.build_context(_ws(admission), admission.credential.user_id or "")
    ent = await entitlements.entitlements_for(ctx.workspace)
    return _checked(reads.WhoAmI, {"workspace_id": ctx.workspace_id, "workspace_name": ctx.workspace.get("name") or "", "role": ctx.role, "plan": ent.plan_name})


@handler("credits.summary")
async def credits_summary(admission: Admission) -> dict[str, Any]:
    s = await credits.summary(_ws(admission))
    return _checked(reads.CreditsSummary, {
        "period": s["period"], "resets_on": s["resets_on"], "plan_name": s["plan_name"], "enforced": s["enforced"], "allowance": s.get("allowance"),
        "used": s["used"], "reserved": s["reserved"], "left": s.get("left"), "percent": s.get("percent"), "state": s.get("state"), "owner_limit": s.get("owner_limit"),
    })


@handler("credits.prices")
async def credits_prices(admission: Admission) -> dict[str, Any]:
    table = await credit_costs.costs()
    rows = []
    for key, cost in table.items():
        row = credit_costs.public_row(key, cost)
        rows.append({"key": key, "label": row["label"], "module": row["module"], "credits": row["credits"], "per": row.get("per"),
                     "per_size": row.get("per_size"), "kind": row["kind"]})
    return _checked(reads.Prices, {"prices": rows})


@handler("plan.limits")
async def plan_limits(admission: Admission) -> dict[str, Any]:
    s = await entitlements.summary(await entitlements.entitlements_for(_ws(admission)))
    rows = [{"key": r["key"], "label": r["label"], "unit": r["unit"], "limit": r.get("allowed"), "used": r.get("used")} for r in s["limits"]]
    return _checked(reads.PlanLimits, {"plan": s["plan_name"], "enforced": s["enforced"], "limits": rows})


@handler("brands.list")
async def brands_list(admission: Admission) -> dict[str, Any]:
    docs = await get_db()["brand_profiles"].find({"workspace_id": _ws(admission)}).sort([("created_at", 1), ("id", 1)]).to_list(length=100)
    rows = []
    for d in docs:
        identity = d.get("identity") or {}
        name = next((str(identity[k]) for k in ("name", "brand_name", "business_name", "full_name") if identity.get(k)), "")
        rows.append({"id": d["id"], "name": name or f"{d.get('brand_type', 'Brand')} voice", "type": str(d.get("brand_type") or ""), "is_default": bool(d.get("is_default"))})
    return _checked(reads.Brands, {"brands": rows})


# ── posts, with a cursor on (time, id) ───────────────────────────────────

def _stage_query(stage: str) -> dict[str, Any]:
    from app.pipelines.text.storage import _stage_query as stage_query

    return stage_query(stage)


def _post_row(p: dict[str, Any]) -> dict[str, Any]:
    from app.pipelines.text.storage import compute_kanban_stage

    return {"id": p["piece_id"], "platform": str(p.get("platform") or ""), "status": compute_kanban_stage(p), "excerpt": str(p.get("content") or "")[:300],
            "created_at": _iso(p.get("created_at"))}


async def _newest_first(workspace_id: str, query: dict[str, Any], limit: int, cursor: Optional[str]) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Pieces newest first. The cursor holds the time and id of the last one given, so the next page starts right after it."""
    q: dict[str, Any] = {"workspace_id": workspace_id, "deleted": {"$ne": True}, **query}
    after = _decode(cursor)
    if after:
        try:
            when = datetime.fromisoformat(str(after[0]).replace("Z", "+00:00"))
        except ValueError:
            raise ToolFailure("input_invalid", "That cursor is not valid. Start again without one.", 422)
        q["$or"] = [{"created_at": {"$lt": when}}, {"created_at": when, "piece_id": {"$lt": str(after[1])}}]
    rows = await get_db()["content_pieces"].find(q).sort([("created_at", -1), ("piece_id", -1)]).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (_encode(_iso(rows[-1]["created_at"]), rows[-1]["piece_id"]) if more and rows else None)


@handler("library.posts")
async def library_posts(admission: Admission) -> dict[str, Any]:
    a = _args(admission)
    query: dict[str, Any] = {}
    if a.get("platform"):
        query["platform"] = a["platform"]
    if a.get("stage"):
        query.update(_stage_query(a["stage"]))
    if a.get("q"):
        import re

        query["content"] = {"$regex": re.escape(a["q"]), "$options": "i"}
    rows, nxt = await _newest_first(_ws(admission), query, a.get("limit", 20), a.get("cursor"))
    return _checked(reads.PostPage, {"items": [_post_row(r) for r in rows], "next_cursor": nxt})


@handler("library.post")
async def library_post(admission: Admission) -> dict[str, Any]:
    from app.pipelines.text.storage import get_piece

    p = await get_piece(_args(admission)["id"], _ws(admission))
    if not p:
        raise ToolFailure("upstream_404", "That post was not found.", 404)
    sched = p.get("publish_scheduled_at") or p.get("scheduled_at")
    return _checked(reads.PostDetail, {
        "id": p["piece_id"], "platform": str(p.get("platform") or ""), "content": str(p.get("content") or ""),
        "approval_status": p.get("approval_status") if p.get("approval_status") in ("pending", "approved", "rejected") else "pending",
        "publish_status": str(p.get("publish_status") or ""), "scheduled_at": _iso(sched) if sched else None, "created_at": _iso(p.get("created_at")),
    })


@handler("approvals.waiting")
async def approvals_waiting(admission: Admission) -> dict[str, Any]:
    a = _args(admission)
    query: dict[str, Any] = {"approval_status": "pending", "archived": {"$ne": True}}
    if a.get("platform"):
        query["platform"] = a["platform"]
    rows, nxt = await _newest_first(_ws(admission), query, a.get("limit", 20), a.get("cursor"))
    items = []
    for r in rows:
        reason = r.get("safety_reason")
        why = (reason.get("message") or reason.get("reason")) if isinstance(reason, dict) else (str(reason) if reason else None)
        items.append({"id": r["piece_id"], "platform": str(r.get("platform") or ""), "excerpt": str(r.get("content") or "")[:300],
                      "created_at": _iso(r.get("created_at")), "needs_review_reason": why or ("Held for review" if r.get("flagged_for_review") else None)})
    return _checked(reads.WaitingPage, {"items": items, "next_cursor": nxt})


@handler("calendar.upcoming")
async def calendar_upcoming(admission: Admission) -> dict[str, Any]:
    """Posts queued to go out, soonest first, between two dates (today to two weeks when none are given)."""
    a = _args(admission)
    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(a["start"]).replace(tzinfo=timezone.utc) if a.get("start") else now
    end = (datetime.fromisoformat(a["end"]).replace(tzinfo=timezone.utc) + timedelta(days=1)) if a.get("end") else start + timedelta(days=14)
    window: dict[str, Any] = {"$gte": start, "$lt": end}
    q: dict[str, Any] = {"workspace_id": _ws(admission), "deleted": {"$ne": True}, "archived": {"$ne": True},
                         "publish_status": {"$in": list(_QUEUED_LIKE)}, "publish_scheduled_at": window}
    after = _decode(a.get("cursor"))
    if after:
        # start right after the last one given: a later time, or the same time with a later id
        when = datetime.fromisoformat(str(after[0]).replace("Z", "+00:00"))
        q["$and"] = [{"$or": [{"publish_scheduled_at": {"$gt": when}}, {"publish_scheduled_at": when, "piece_id": {"$gt": str(after[1])}}]}]
    limit = a.get("limit", 20)
    rows = await get_db()["content_pieces"].find(q).sort([("publish_scheduled_at", 1), ("piece_id", 1)]).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    items = [{"id": r["piece_id"], "platform": str(r.get("platform") or ""), "excerpt": str(r.get("content") or "")[:300], "scheduled_at": _iso(r["publish_scheduled_at"]),
              "approval_status": r.get("approval_status") if r.get("approval_status") in ("pending", "approved", "rejected") else "pending"} for r in rows]
    nxt = _encode(_iso(rows[-1]["publish_scheduled_at"]), rows[-1]["piece_id"]) if more and rows else None
    return _checked(reads.ScheduledPage, {"items": items, "next_cursor": nxt})


@handler("campaigns.list")
async def campaigns_list(admission: Admission) -> dict[str, Any]:
    a = _args(admission)
    q: dict[str, Any] = {"workspace_id": _ws(admission), "deleted": {"$ne": True}, "kind": {"$ne": "repurpose"}}
    if a.get("status"):
        q["status"] = a["status"]
    after = _decode(a.get("cursor"))
    if after:
        when = datetime.fromisoformat(str(after[0]).replace("Z", "+00:00"))
        q["$or"] = [{"updated_at": {"$lt": when}}, {"updated_at": when, "id": {"$lt": str(after[1])}}]
    limit = a.get("limit", 20)
    rows = await get_db()["campaigns"].find(q).sort([("updated_at", -1), ("id", -1)]).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    items = [{"id": r["id"], "name": r.get("name") or "", "status": str(getattr(r.get("status"), "value", r.get("status")) or "draft"),
              "platforms": [str(p) for p in (r.get("platforms") or [])], "created_at": _iso(r.get("created_at"))} for r in rows]
    nxt = _encode(_iso(rows[-1].get("updated_at")), rows[-1]["id"]) if more and rows else None
    return _checked(reads.CampaignPage, {"items": items, "next_cursor": nxt})


@handler("runs.list")
async def runs_list(admission: Admission) -> dict[str, Any]:
    a = _args(admission)
    rows = await pipeline_runs.list_runs(_ws(admission))
    if a.get("status"):
        rows = [r for r in rows if r.get("status") == a["status"]]
    after = _decode(a.get("cursor"))
    if after:
        ids = [r["id"] for r in rows]
        rows = rows[ids.index(str(after[1])) + 1:] if str(after[1]) in ids else rows
    limit = a.get("limit", 20)
    more = len(rows) > limit
    rows = rows[:limit]
    known = {"queued", "running", "paused", "done", "failed", "cancelled"}
    items = []
    for r in rows:
        pub = pipeline_runs.public(r)
        items.append({"id": pub["id"], "kind": str(pub.get("kind") or ""), "title": str(pub.get("title") or ""),
                      "status": pub["status"] if pub.get("status") in known else "failed", "progress_percent": pub.get("progress_percent"),
                      "href": pub.get("href"), "can_retry": bool(pub.get("can_retry"))})
    nxt = _encode("run", rows[-1]["id"]) if more and rows else None
    return _checked(reads.RunPage, {"items": items, "next_cursor": nxt})


@handler("analytics.top_assets")
async def top_assets(admission: Admission) -> dict[str, Any]:
    from app.tools.executor import request_route

    a = _args(admission)
    data = await request_route(admission.credential, "GET", f"/api/v1/analytics/assets/{a['kind']}/top", {"limit": a.get("limit", 10)}, None)
    items = [{"rank": i["rank"], "id": i["asset_id"], "title": i.get("title") or "", "average_engagement_rate": i["average_engagement_rate"],
              "measured_posts": i["measured_posts"]} for i in data.get("items", [])]
    return _checked(reads.TopAssets, {"items": items})
