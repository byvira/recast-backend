"""What a workspace may use: one answer for every limit in the product.

Every place that creates something a plan limits asks here (`require_room`), so no route, agent, campaign, automation or API call
can get round a limit. Workflow (whether posts wait for review, team rules) stays in `tier_policy`; this module only says how much.

Two modes, chosen by `settings.BILLING_ENFORCED`:
- Off (until launch): every limit is exactly what it was before plans existed. Seats come from the workspace's team size, AI
  pictures and video use the shared daily and monthly ceilings, brand voices and API keys keep their fixed caps, and nothing else
  is limited. A workspace counts only its own usage.
- On: each workspace gets its plan's limits from the plans table. A plan belongs to a paid workspace; other workspaces its owner
  links to it (`billing_group_id`) share that plan and its usage, so extra workspaces never multiply an allowance.

Per-workspace overrides set from Ops (`entitlement_overrides`: a complimentary plan, extra limits, an end date) apply in both modes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.billing import plans as P
from app.core.config import settings
from app.db.mongo import get_db, workspaces

PLAN_CACHE_SECONDS = 60
ENTITLEMENT_CACHE_SECONDS = 10

# Limits that already existed before plans, with the value each had. Every other limit is open while plans are off.
_LEGACY_FIXED: dict[str, int] = {"brand_voices": 10, "api_keys": 10, "review_link_days": 14, "render_priority": 0, "support_priority": 0}

_WS_FIELDS = {
    "id": 1, "tier": 1, "tier_config": 1, "plan": 1, "plan_seats": 1, "billing_group_id": 1, "entitlement_overrides": 1,
    "owner_id": 1, "is_personal": 1, "deleted_at": 1,
}


@dataclass
class Entitlements:
    workspace_id: str
    enforced: bool
    plan: str
    plan_name: str
    seats_billed: int
    group_id: str
    scope_ids: list[str]
    limits: dict[str, Optional[int]]
    plan_limits: dict[str, Optional[int]]
    comp: bool = False
    override_note: Optional[str] = None
    override_expires_at: Optional[datetime] = None
    tier: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def limit(self, key: str) -> Optional[int]:
        return self.limits.get(key)


# ── the plans table ──────────────────────────────────────────────────────

_plans_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _plans_collection():
    return get_db()["billing_plans"]


async def load_plans() -> dict[str, dict[str, Any]]:
    """The plans table: the defaults with any values saved from Ops on top. Cached for a minute; `forget_plans` clears it."""
    global _plans_cache
    now = time.monotonic()
    if _plans_cache and _plans_cache[0] > now:
        return _plans_cache[1]
    table = {pid: {**plan, "limits": dict(plan["limits"])} for pid, plan in P.DEFAULT_PLANS.items()}
    try:
        async for saved in _plans_collection().find({"id": {"$in": list(P.PLAN_IDS)}}):
            plan = table[saved["id"]]
            for key, rule in (saved.get("limits") or {}).items():
                if key in P.LIMITS:
                    plan["limits"][key] = rule
            for key in ("name", "min_seats", "max_seats"):
                if saved.get(key) is not None:
                    plan[key] = saved[key]
    except Exception:  # noqa: BLE001 - the defaults are always a safe answer
        pass
    _plans_cache = (now + PLAN_CACHE_SECONDS, table)
    return table


def forget_plans() -> None:
    global _plans_cache
    _plans_cache = None
    forget()


# ── one workspace's entitlements ─────────────────────────────────────────

_cache: dict[str, tuple[float, Entitlements]] = {}


def forget(workspace_id: Optional[str] = None) -> None:
    """Drop cached answers (all, or one workspace and anything sharing its plan), after a plan or override changes."""
    if workspace_id is None:
        _cache.clear()
        return
    for key, (_, ent) in list(_cache.items()):
        if key == workspace_id or ent.group_id == workspace_id:
            _cache.pop(key, None)


def _active_override(ws: dict) -> dict:
    o = ws.get("entitlement_overrides") or {}
    expires = o.get("expires_at")
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return {}
    return o


def legacy_limits(ws: dict) -> dict[str, Optional[int]]:
    """The limits as they were before plans: what every workspace gets while plans are off."""
    from app.core import limits as server_limits
    from app.video import limits as video_limits

    video = video_limits.limits_for(ws)
    out: dict[str, Optional[int]] = {key: None for key in P.LIMITS}
    out.update(_LEGACY_FIXED)
    out["seats"] = int((ws.get("tier_config") or {}).get("seats") or 1)
    out["pictures_day"] = int(server_limits.IMAGES_PER_WORKSPACE_PER_DAY)
    out["render_minutes_month"] = int(video["render_minutes"])
    out["storage_mb"] = int(video["storage_mb"])  # video files only, as before
    return out


async def group_ids(group_id: str) -> list[str]:
    ids = [group_id]
    async for w in workspaces.find({"billing_group_id": group_id, "deleted_at": {"$exists": False}}, {"id": 1}):
        if w["id"] != group_id:
            ids.append(w["id"])
    return ids


async def entitlements_for(workspace: dict | str) -> Entitlements:
    """Everything this workspace may use. Pass the workspace document or its id."""
    ws_id = workspace if isinstance(workspace, str) else workspace.get("id")
    now = time.monotonic()
    hit = _cache.get(ws_id) if ws_id else None
    if hit and hit[0] > now:
        return hit[1]

    ws = await workspaces.find_one({"id": ws_id}, _WS_FIELDS) if isinstance(workspace, str) or "tier_config" not in workspace else workspace
    if not ws:
        # No saved workspace (a job started for one since removed, or a background caller with only an id): the open defaults,
        # as the older checks gave.
        ws = {"id": ws_id}
    table = await load_plans()
    enforced = bool(settings.BILLING_ENFORCED)

    group_id = ws.get("billing_group_id") or ws["id"]
    payer = ws
    if group_id != ws["id"]:
        payer = await workspaces.find_one({"id": group_id, "deleted_at": {"$exists": False}}, _WS_FIELDS) or ws
        group_id = payer["id"]

    override = _active_override(payer)
    own_override = _active_override(ws) if payer is not ws else {}
    plan_id = override.get("plan") or payer.get("plan") or "free"
    if plan_id not in table:
        plan_id = "free"
    plan = table[plan_id]
    seats = P.clamp_seats(plan, override.get("seats") or payer.get("plan_seats"))
    plan_limits = P.limits_for(plan, seats)
    for key, value in {**(override.get("limits") or {}), **(own_override.get("limits") or {})}.items():
        if key in plan_limits:
            plan_limits[key] = P.resolve(value, seats)

    if enforced:
        limits = dict(plan_limits)
        scope = await group_ids(group_id)
    else:
        # While plans are off, a limit Ops set on this workspace itself still applies (a comped plan only matters once on).
        limits = legacy_limits(ws)
        for key, value in (_active_override(ws).get("limits") or {}).items():
            if key in limits:
                limits[key] = P.resolve(value, seats)
        scope = [ws["id"]]

    ent = Entitlements(
        workspace_id=ws["id"], enforced=enforced, plan=plan_id, plan_name=str(plan.get("name") or plan_id.title()),
        seats_billed=seats, group_id=group_id, scope_ids=scope, limits=limits, plan_limits=plan_limits,
        comp=bool(override.get("plan")), override_note=override.get("note") or own_override.get("note"),
        override_expires_at=override.get("expires_at") or own_override.get("expires_at"), tier=str(ws.get("tier") or ""),
    )
    _cache[ws["id"]] = (now + ENTITLEMENT_CACHE_SECONDS, ent)
    return ent


# ── checking room ────────────────────────────────────────────────────────

_HINTS: dict[str, str] = {
    "seats": "Remove a member or an open invite, or add seats.",
    "owned_workspaces": "Remove a workspace you no longer use, or move to a plan with more.",
    "social_accounts": "Disconnect one you no longer post to, or move to a plan with more.",
    "brand_voices": "Remove a voice you no longer use, or move to a plan with more.",
    "credits_month": "Your credits start again on your renewal date.",
    "render_minutes_month": "Your minutes start again at the start of your plan month.",
    "pictures_day": "Try again tomorrow, or make fewer.",
    "storage_mb": "Remove files you no longer need, or move to a plan with more space.",
    "scheduled_ahead": "Wait for some to go out, or move to a plan with more.",
    "campaigns_active": "Pause one first, or move to a plan with more.",
    "video_automations": "Turn one off first, or move to a plan with more.",
    "api_keys": "Remove a key you no longer use first.",
}


def _size(mb: float) -> str:
    return f"{mb / 1024:,.1f} GB".replace(".0 GB", " GB") if mb >= 1024 else f"{int(mb):,} MB"


def limit_message(ent: Entitlements, key: str, used: float, adding: float = 1) -> str:
    """Plain words for a limit that is reached, for example "The Single plan includes 10 connected social accounts. All are in use.
    Disconnect one you no longer post to, or move to a plan with more."."""
    meta = P.LIMITS[key]
    allowed = ent.limits.get(key) or 0
    left = max(0.0, allowed - used)
    who = f"The {ent.plan_name} plan" if ent.enforced else "This workspace"
    if key == "storage_mb":
        head, state = f"{who} includes {_size(allowed)} of storage", (f"Only {_size(left)} is left." if left > 0 else "It is full.")
    else:
        per = " a day" if key.endswith("_day") else " a month" if key.endswith("_month") else ""
        label = meta["label"]
        # "Brand voices" reads "brand voices" mid-sentence; "API keys" keeps its capitals
        noun = (label if label[:2].isupper() else label[0].lower() + label[1:]) if per == "" else meta["unit"]
        head = f"{who} includes {allowed:,} {noun}{per}"
        if left >= 1:
            state = f"You have {int(left):,} left."
        else:
            state = "None are left." if per else "All are in use."
    return f"{head}. {state} {_HINTS.get(key, '')}".strip()


def limit_detail(ent: Entitlements, key: str, used: float, adding: float, message: Optional[str] = None) -> dict[str, Any]:
    return {
        "code": "limit_reached",
        "limit": key,
        "allowed": ent.limits.get(key),
        "used": used,
        "adding": adding,
        "plan": ent.plan if ent.enforced else None,
        "enforced": ent.enforced,
        "message": message or limit_message(ent, key, used, adding),
    }


def has_room(ent: Entitlements, key: str, used: float, adding: float = 1) -> bool:
    allowed = ent.limits.get(key)
    return allowed is None or used + adding <= allowed


async def require_room(
    ent: Entitlements, key: str, used: Optional[float] = None, adding: float = 1, *,
    legacy_status: int = 400, legacy_message: Optional[str] = None, status: int = 402,
) -> None:
    """Raise a plain-words error when adding this much would go past the limit. `used` is counted here when not given.

    While plans are off, a limit that existed before plans answers with the same status and words it always did
    (`legacy_status`, `legacy_message`), so nothing a member sees changes until launch."""
    if ent.limits.get(key) is None:
        return
    if used is None:
        used = await count_usage(ent, key)
    if has_room(ent, key, used, adding):
        return
    if not ent.enforced and legacy_message is not None:
        raise HTTPException(status_code=legacy_status, detail=legacy_message)
    raise HTTPException(status_code=status if ent.enforced else legacy_status, detail=limit_detail(ent, key, used, adding))


# ── usage counters (over every workspace sharing the plan) ───────────────

def _in(ids: list[str]) -> dict[str, Any]:
    return {"$in": ids} if len(ids) > 1 else ids[0]


def _day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def seats_used(ent: Entitlements) -> int:
    """Active members plus invites still open. While plans are off this is one workspace, as before. Once they are on, seats
    belong to the plan: each person counts once across every workspace sharing it, and an open invite to someone who is already
    a member somewhere in the group holds no extra seat."""
    db = get_db()
    now = datetime.now(timezone.utc)
    if not ent.enforced:
        active = await db["workspace_members"].count_documents({"workspace_id": ent.workspace_id, "status": "active"})
        open_invites = await db["invites"].count_documents({"workspace_id": ent.workspace_id, "status": "pending", "expires_at": {"$gt": now}})
        return active + open_invites
    scope = _in(ent.scope_ids)
    people = set(await db["workspace_members"].distinct("user_id", {"workspace_id": scope, "status": "active"}))
    emails: set[str] = set()
    async for u in db["users"].find({"id": {"$in": list(people)}}, {"email": 1, "auth_identifiers": 1}):
        emails.update(e.lower() for e in [u.get("email") or "", *(u.get("auth_identifiers") or [])] if e)
    invited = {
        str(e).lower() for e in await db["invites"].distinct("email", {"workspace_id": scope, "status": "pending", "expires_at": {"$gt": now}})
    }
    return len(people) + len(invited - emails)


async def holds_seat(ent: Entitlements, user_id: str) -> bool:
    """True when this person already counts against the plan's seats (a member of another workspace sharing it)."""
    if not ent.enforced:
        return False
    return bool(await get_db()["workspace_members"].find_one({"workspace_id": _in(ent.scope_ids), "user_id": user_id, "status": "active"}, {"_id": 1}))


async def count_usage(ent: Entitlements, key: str) -> float:
    db = get_db()
    ws = _in(ent.scope_ids)
    if key == "seats":
        return await seats_used(ent)
    if key == "owned_workspaces":
        return len(ent.scope_ids)
    if key == "social_accounts":
        return await db["workspace_connections"].count_documents(
            {"workspace_id": ws, "is_active": True, "platform": {"$nin": list(NOT_COUNTED_CONNECTIONS)}}
        )
    if key == "brand_voices":
        return await db["brand_profiles"].count_documents({"workspace_id": ws})
    if key == "pictures_day":
        return await db["media_assets"].count_documents({"workspace_id": ws, "source": "ai_generated", "created_at": {"$gte": _day_start()}})
    if key == "render_minutes_month":
        from app.video import limits as video_limits

        total = 0.0
        for wid in ent.scope_ids:
            total += await video_limits.render_seconds_used(wid)
        return round(total / 60.0, 2)
    if key == "storage_mb":
        match: dict[str, Any] = {"workspace_id": ws}
        if not ent.enforced:
            match["kind"] = "video"
        rows = await db["media_assets"].aggregate([
            {"$match": match}, {"$group": {"_id": None, "bytes": {"$sum": {"$ifNull": ["$size_bytes", 0]}}}},
        ]).to_list(length=1)
        return round((int(rows[0]["bytes"]) if rows else 0) / 1_000_000.0, 2)
    if key == "scheduled_ahead":
        return await db["content_pieces"].count_documents(
            {"workspace_id": ws, "deleted": {"$ne": True}, "publish_status": {"$in": ["queued", "scheduled"]}}
        )
    if key == "campaigns_active":
        return await db["campaigns"].count_documents(
            {"workspace_id": ws, "deleted": {"$ne": True}, "status": "active", "kind": {"$ne": "repurpose"}}
        )
    if key == "video_automations":
        return await db["video_recipes"].count_documents({"workspace_id": ws, "automation.enabled": True})
    if key == "api_keys":
        return await db["api_keys"].count_documents({"workspace_id": ws, "revoked": {"$ne": True}})
    return 0


# The Google sign-in is kept as its own connection behind YouTube; YouTube itself is the account that counts.
NOT_COUNTED_CONNECTIONS = ("google",)


async def connection_room_message(workspace_id: str, platform: str) -> Optional[str]:
    """Plain words when connecting this platform would go past the plan's social accounts, else None. Reconnecting an account the
    workspace already has never counts again."""
    if platform in NOT_COUNTED_CONNECTIONS:
        return None
    if await get_db()["workspace_connections"].find_one({"workspace_id": workspace_id, "platform": platform, "is_active": True}, {"_id": 1}):
        return None
    ent = await entitlements_for(workspace_id)
    try:
        await require_room(ent, "social_accounts")
    except HTTPException as exc:
        detail = exc.detail
        return detail.get("message") if isinstance(detail, dict) else str(detail)
    return None


async def require_connection_room(workspace_id: str, platform: str) -> None:
    """The same check as an error, for routes that answer with JSON."""
    if platform in NOT_COUNTED_CONNECTIONS:
        return
    if await get_db()["workspace_connections"].find_one({"workspace_id": workspace_id, "platform": platform, "is_active": True}, {"_id": 1}):
        return
    await require_room(await entitlements_for(workspace_id), "social_accounts")


async def require_campaign_room(workspace_id: str, campaign: dict) -> None:
    """A campaign that is about to start running (from draft or paused) must fit the plan's campaigns running at once. A repurpose
    is made once and never counts; one already running is fine."""
    if campaign.get("kind") == "repurpose" or campaign.get("status") == "active":
        return
    await require_room(await entitlements_for(workspace_id), "campaigns_active")


async def require_storage_room(workspace_id: str, adding_bytes: int) -> None:
    """Every file a member uploads must fit the plan's storage once plans are on. While they are off only videos have a space
    (checked by `app.video.limits.assert_storage_room`), so this checks nothing. Files Ekavo makes are always kept, so finished work
    is never lost; a full plan stops new uploads until space is freed."""
    ent = await entitlements_for(workspace_id)
    if not ent.enforced:
        return
    room = ent.limit("storage_mb")
    if room is None:
        return
    used = await count_usage(ent, "storage_mb")
    adding = max(0, int(adding_bytes or 0)) / 1_000_000.0
    if used + adding > room:
        raise HTTPException(status_code=402, detail=limit_detail(ent, "storage_mb", used, adding))


async def picture_room_message(workspace_id: str, wanted: int = 1) -> Optional[str]:
    """Plain words when making `wanted` more AI pictures today would go past the daily allowance, else None. Never raises: the
    picture makers report it as the reason no picture was made."""
    try:
        ent = await entitlements_for(workspace_id)
        await require_room(ent, "pictures_day", adding=wanted, legacy_status=429, legacy_message=None)
    except HTTPException as exc:
        return exc.detail.get("message") if isinstance(exc.detail, dict) else str(exc.detail)
    except Exception:  # noqa: BLE001 - a counting problem never blocks a picture
        return None
    return None


USAGE_KEYS = (
    "seats", "owned_workspaces", "social_accounts", "brand_voices", "pictures_day", "render_minutes_month", "storage_mb",
    "scheduled_ahead", "campaigns_active", "video_automations", "api_keys",
)


async def summary(ent: Entitlements, *, for_ops: bool = False) -> dict[str, Any]:
    """What a screen shows: the plan, and for every limit what is allowed, what the plan gives, and what is used."""
    rows = []
    for key, meta in P.LIMITS.items():
        used = await count_usage(ent, key) if key in USAGE_KEYS else None
        allowed = ent.limits.get(key)
        rows.append({
            "key": key, "label": meta["label"], "unit": meta["unit"], "kind": meta["kind"],
            "allowed": allowed, "plan_allows": ent.plan_limits.get(key), "used": used,
            "state": None if used is None or allowed is None else ("over" if used >= allowed else "near" if allowed and used / allowed >= 0.8 else "ok"),
        })
    return {
        "workspace_id": ent.workspace_id, "enforced": ent.enforced, "plan": ent.plan, "plan_name": ent.plan_name,
        "seats_billed": ent.seats_billed, "group_id": ent.group_id, "group_size": len(ent.scope_ids) if ent.enforced else 1,
        "comp": ent.comp, "override_note": ent.override_note if for_ops else None,  # Ekavo's own note stays in Ops
        "override_expires_at": ent.override_expires_at.isoformat() if ent.override_expires_at else None,
        "limits": rows,
    }
