"""Ekavo staff and what each may do in Ops.

Three levels, the top two fixed here in code so nobody can weaken them from the dashboard:
- Master Admin: everything, and the only one who changes the permission grid, roles, Admins and Master Admins, money settings and
  risky switches (those routes keep checking `is_master_admin`, which only this level carries). There is always at least one.
- Admin: every Ops area, but not Team settings, roles or the grid; may invite and manage staff below Admin only when the Master
  Admin allows it.
- Staff: the areas their role gives (view, edit, approve), plus any exceptions set on the person.

Roles below Admin are records in `staff_roles`, made and changed in Ops, Team. A starting set of 17 is written the first time the
roles are read, 8 of them switched on. Each staff member's level, role, designation and status live on their user record (`staff`),
read on every request (cached a few seconds), so deactivating someone cuts their access at once.

Every Ops route group is guarded by `area_guard(area)` where it is mounted: reading needs "view", any change needs "edit".
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request

from app.core.auth import get_current_user
from app.db.mongo import get_db

LEVELS = ("master_admin", "admin", "staff")
LEVEL_LABEL = {"master_admin": "Master Admin", "admin": "Admin", "staff": "Staff"}
ACTIONS = ("view", "edit", "approve")

# Every Ops area, in the order the permission grid lists them. "soon" areas are on the grid ahead of their pages.
AREAS: dict[str, dict[str, Any]] = {
    "billing": {"label": "Billing and payments"},
    "plans": {"label": "Plans and credits"},
    "support": {"label": "Support"},
    "leads": {"label": "Leads, waitlist and invites"},
    "cohorts": {"label": "Cohorts and rollouts"},
    "banners": {"label": "In-app banners"},
    "prompts": {"label": "Prompt library"},
    "ai": {"label": "AI health and budget"},
    "health": {"label": "Errors and status"},
    "platforms": {"label": "Platforms"},
    "video": {"label": "Video"},
    "platform": {"label": "Platform layer: API, tools and keys"},
    "safety": {"label": "Content safety"},
    "quality": {"label": "Quality checks"},
    "workspaces": {"label": "Workspaces and people"},
    "content": {"label": "Website content"},
    "team": {"label": "Team and access"},
}

ALL = list(ACTIONS)
V = ["view"]
VE = ["view", "edit"]

# The starting roles. "active" ones are on at first; the rest are ready to switch on as people are hired.
TEMPLATES: list[dict[str, Any]] = [
    {"key": "support_agent", "name": "Support Agent", "group": "Customers", "active": True,
     "description": "Answers tickets and fixes account problems.",
     "permissions": {"support": VE, "workspaces": V, "billing": V, "plans": V}},
    {"key": "support_lead", "name": "Support Lead", "group": "Customers", "active": False,
     "description": "Runs the support team: assigning, response times, incidents.",
     "permissions": {"support": ALL, "workspaces": V, "billing": V, "plans": V, "health": V}},
    {"key": "customer_success", "name": "Customer Success", "group": "Customers", "active": True,
     "description": "Onboards and keeps paying customers, especially teams and agencies.",
     "permissions": {"workspaces": VE, "plans": VE, "billing": V, "support": V, "leads": V}},
    {"key": "sales", "name": "Sales & Partnerships", "group": "Revenue", "active": False,
     "description": "Agency and team deals, demos.",
     "permissions": {"leads": VE, "workspaces": V, "plans": V}},
    {"key": "finance", "name": "Finance", "group": "Revenue", "active": True,
     "description": "Payments, refunds, receipts and GST.",
     "permissions": {"billing": ALL, "plans": VE, "workspaces": V}},
    {"key": "marketing", "name": "Marketing", "group": "Revenue", "active": True,
     "description": "Website, pricing page wording and campaigns.",
     "permissions": {"content": ALL, "cohorts": VE, "banners": VE, "leads": V}},
    {"key": "content_writer", "name": "Content Writer", "group": "Content and brand", "active": False,
     "description": "Writes blog posts and page copy; drafts go to an editor.",
     "permissions": {"content": VE}},
    {"key": "content_editor", "name": "Content Editor", "group": "Content and brand", "active": True,
     "description": "Reviews, approves and publishes website content.",
     "permissions": {"content": ALL, "banners": VE}},
    {"key": "people", "name": "People & Hiring", "group": "Content and brand", "active": False,
     "description": "Job posts and applicants.",
     "permissions": {"content": VE, "leads": V}},
    {"key": "product_manager", "name": "Product Manager", "group": "Product and engineering", "active": False,
     "description": "Plans, feature rollouts and what customers use.",
     "permissions": {"plans": V, "cohorts": ALL, "banners": VE, "workspaces": V, "ai": V, "health": V}},
    {"key": "engineer", "name": "Engineer", "group": "Product and engineering", "active": True,
     "description": "Keeps the service running: errors, status, platforms, video.",
     "permissions": {"health": ALL, "platforms": VE, "video": VE, "platform": VE, "ai": V, "prompts": V}},
    {"key": "ai_engineer", "name": "AI Engineer", "group": "Product and engineering", "active": False,
     "description": "Prompts, models and AI cost.",
     "permissions": {"prompts": ALL, "ai": VE, "health": V, "platform": V}},
    {"key": "trust_safety", "name": "Trust & Safety", "group": "Trust and quality", "active": True,
     "description": "Abuse, harmful content and takedowns.",
     "permissions": {"safety": ALL, "quality": V, "workspaces": VE, "support": V}},
    {"key": "quality_analyst", "name": "Quality Analyst", "group": "Trust and quality", "active": True,
     "description": "Tunes the writing checks and watches what they hold back.",
     "permissions": {"quality": VE, "prompts": V, "safety": V, "workspaces": V}},
    {"key": "quality_auditor", "name": "Quality Auditor", "group": "Trust and quality", "active": False,
     "description": "Checks brand and content quality across customers.",
     "permissions": {"workspaces": VE, "safety": V}},
    {"key": "legal", "name": "Legal & Privacy", "group": "Trust and quality", "active": False,
     "description": "Data requests, legal pages and compliance.",
     "permissions": {"support": V, "workspaces": V, "content": VE}},
    {"key": "analyst", "name": "Analyst", "group": "Read only", "active": False,
     "description": "Numbers and reports, read only.",
     "permissions": {k: V for k in AREAS if k != "team"}},
    {"key": "viewer", "name": "Viewer", "group": "Read only", "active": True,
     "description": "Investors, advisors, new joiners: only what they are given, read only.",
     "permissions": {}},
]

ACCESS_CACHE_SECONDS = 5


def roles_col():
    return get_db()["staff_roles"]


def invites_col():
    return get_db()["staff_invites"]


def audit_col():
    return get_db()["staff_audit"]


def settings_col():
    return get_db()["staff_settings"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_permissions(perms: Any) -> dict[str, list[str]]:
    """Only known areas and actions, in a fixed order; "edit" or "approve" always bring "view"."""
    out: dict[str, list[str]] = {}
    for area, actions in (perms or {}).items():
        if area not in AREAS or not isinstance(actions, (list, tuple)):
            continue
        chosen = {a for a in actions if a in ACTIONS}
        if chosen:
            chosen.add("view")
            out[area] = [a for a in ACTIONS if a in chosen]
    return out


async def ensure_templates() -> None:
    """Write any starting role that is missing, so a custom role made first never leaves the starting ones out. A starting role
    the Master Admin deleted is remembered and never written again."""
    have = {r["id"] async for r in roles_col().find({}, {"_id": 0, "id": 1})}
    if all(t["key"] in have for t in TEMPLATES):
        return
    removed = set(((await settings_col().find_one({"_id": "templates"})) or {}).get("removed") or [])
    now = _now()
    docs = [{
        "id": t["key"], "key": t["key"], "name": t["name"], "group": t["group"], "description": t["description"],
        "permissions": clean_permissions(t["permissions"]), "active": t["active"], "template": True,
        "created_at": now, "updated_at": now, "updated_by": "system",
    } for t in TEMPLATES if t["key"] not in have and t["key"] not in removed]
    if not docs:
        return
    try:
        await roles_col().insert_many(docs, ordered=False)
    except Exception:  # noqa: BLE001 - another server wrote them at the same moment
        pass


async def get_settings() -> dict[str, Any]:
    doc = await settings_col().find_one({"_id": "team"}) or {}
    return {"admins_may_invite": bool(doc.get("admins_may_invite", False))}


_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def forget(user_id: Optional[str] = None) -> None:
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)


async def access_for(user: dict[str, Any]) -> dict[str, Any]:
    """What this person may do in Ops: their level, role and the actions per area. Not staff: level None and no areas."""
    uid = str(user.get("id") or "")
    hit = _cache.get(uid)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    staff = user.get("staff") or {}
    if staff.get("status") == "deactivated":
        result: dict[str, Any] = {"level": None, "areas": {}}
    elif user.get("is_master_admin"):
        result = {"level": "master_admin", "areas": {a: list(ACTIONS) for a in AREAS}}
    elif user.get("is_platform_staff"):
        level = staff.get("level") or "admin"  # staff set by hand before levels existed count as Admins
        if level == "admin":
            settings = await get_settings()
            areas = {a: list(ACTIONS) for a in AREAS if a != "team"}
            areas["team"] = ["view", "edit"] if settings["admins_may_invite"] else ["view"]
            result = {"level": "admin", "areas": areas}
        else:
            role = await roles_col().find_one({"id": staff.get("role_id"), "active": True}) if staff.get("role_id") else None
            perms = clean_permissions((role or {}).get("permissions"))
            for area, actions in clean_permissions(staff.get("exceptions")).items():
                perms[area] = [a for a in ACTIONS if a in set(perms.get(area, [])) | set(actions)]
            perms.pop("team", None)  # staff below Admin never manage the team
            result = {"level": "staff", "areas": perms, "role_id": staff.get("role_id"), "role_name": (role or {}).get("name")}
    else:
        result = {"level": None, "areas": {}}
    result.setdefault("designation", staff.get("designation"))
    _cache[uid] = (time.monotonic() + ACCESS_CACHE_SECONDS, result)
    return result


def can(access: dict[str, Any], area: str, action: str = "view") -> bool:
    return action in (access.get("areas") or {}).get(area, [])


def area_guard(area: str):
    """Mounted on each Ops route group: reading needs "view" on the area, any change needs "edit"."""

    async def _dep(request: Request, user: dict = Depends(get_current_user)) -> dict:
        access = await access_for(user)
        if not access.get("level"):
            raise HTTPException(status_code=403, detail="Platform staff access required.")
        action = "view" if request.method in ("GET", "HEAD", "OPTIONS") else "edit"
        if not can(access, area, action):
            what = "see" if action == "view" else "change"
            raise HTTPException(status_code=403, detail=f"You don't have access to {what} {AREAS[area]['label'].lower()}. Ask your Admin.")
        return user

    return _dep


def require_action(area: str, action: str):
    """For a single route that needs more than its area's guard gives, for example "approve" to refund or publish."""

    async def _dep(user: dict = Depends(get_current_user)) -> dict:
        if not can(await access_for(user), area, action):
            raise HTTPException(status_code=403, detail=f"This needs {action} access to {AREAS[area]['label'].lower()}. Ask your Admin.")
        return user

    return _dep


def team_record(user: dict[str, Any]) -> bool:
    """Whether the person is on Team (invited or set there), as against staff set by hand before Team existed."""
    return bool((user.get("staff") or {}).get("level"))


async def support_role_for(user: dict[str, Any]) -> Optional[str]:
    """The Support module's own rank (agent, lead, admin), from Team: Admins and Master Admins are Support admins, Approve on
    Support is a lead, any other Support access is an agent, none is nothing."""
    access = await access_for(user)
    level = access.get("level")
    if level in ("master_admin", "admin"):
        return "admin"
    if can(access, "support", "approve"):
        return "lead"
    if can(access, "support", "view"):
        return "agent"
    return None


async def sync_module_roles(user_id: str) -> None:
    """Write the ranks older modules read straight from the user record (Support's `support_role`), so Team stays the one place
    access is set. Called after every Team change that can move someone's access."""
    from app.db.mongo import users

    forget(user_id)
    user = await users.find_one({"id": user_id})
    if not user or not team_record(user):
        return
    role = await support_role_for(user)
    if role:
        await users.update_one({"id": user_id}, {"$set": {"support_role": role}})
    else:
        await users.update_one({"id": user_id}, {"$unset": {"support_role": ""}})


async def sync_role_members(role_id: str) -> None:
    from app.db.mongo import users

    async for u in users.find({"staff.role_id": role_id}, {"id": 1}):
        await sync_module_roles(u["id"])


async def audit(by: dict[str, Any], action: str, target: str, *, before: Any = None, after: Any = None, reason: str = "") -> None:
    await audit_col().insert_one({
        "at": _now(), "by": by.get("id"), "by_email": by.get("email"), "action": action, "target": target,
        "before": before, "after": after, "reason": reason,
    })


async def create_indexes() -> None:
    await roles_col().create_index("id", unique=True)
    await invites_col().create_index("token", unique=True)
    await invites_col().create_index([("email", 1), ("status", 1)])
    await invites_col().create_index("expires_at")
    await audit_col().create_index([("at", -1)])
