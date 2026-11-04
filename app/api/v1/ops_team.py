"""Ops, Team: staff, their designations and roles, invites, the permission grid and the history of every change.

Who may do what here (app.core.staff has the levels):
- Master Admin: everything; the only one who changes roles, the grid, exceptions, Admins and Master Admins, and Team settings.
- Admin: sees the team; invites, changes and deactivates staff below Admin only when the Master Admin allows it.
- Staff: none of it. They only read their own access (/me).
Nobody gives access equal to or above their own, nobody changes their own access, and there is always at least one Master Admin.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core import staff as ST
from app.core.auth import get_current_user
from app.core.config import settings
from app.core.exports import csv_response
from app.core.middleware import limiter
from app.db.mongo import users

logger = logging.getLogger(__name__)
router = APIRouter()

INVITE_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _team_access(user: dict = Depends(get_current_user)) -> tuple[dict, dict]:
    access = await ST.access_for(user)
    if not ST.can(access, "team", "view"):
        raise HTTPException(status_code=403, detail="You don't have access to the team. Ask your Admin.")
    return user, access


def _master(access: dict) -> None:
    if access.get("level") != "master_admin":
        raise HTTPException(status_code=403, detail="Only the Master Admin can do this.")


def _may_manage(access: dict, target_level: str) -> None:
    """Master Admin manages anyone; an Admin only staff below Admin, and only when allowed."""
    if access.get("level") == "master_admin":
        return
    if access.get("level") == "admin" and ST.can(access, "team", "edit") and target_level == "staff":
        return
    raise HTTPException(status_code=403, detail="You can't give or change access at this level. Ask the Master Admin.")


async def _role_is_on(role_id: str | None) -> bool:
    """Whether a role exists and is switched on. The starting roles are written first, so a fresh database can invite at once."""
    if not role_id:
        return False
    await ST.ensure_templates()
    return bool(await ST.roles_col().find_one({"id": role_id, "active": True}))


async def _masters_count(exclude: Optional[str] = None) -> int:
    q: dict[str, Any] = {"is_master_admin": True, "staff.status": {"$ne": "deactivated"}}
    if exclude:
        q["id"] = {"$ne": exclude}
    return await users.count_documents(q)


def _level_of(u: dict) -> Optional[str]:
    if (u.get("staff") or {}).get("status") == "deactivated":
        return (u.get("staff") or {}).get("level") or "staff"
    if u.get("is_master_admin"):
        return "master_admin"
    if u.get("is_platform_staff"):
        return (u.get("staff") or {}).get("level") or "admin"
    return None


async def _tell_masters(template: str, variables: dict[str, Any]) -> None:
    from app.core.notifications import send_templated_email

    try:
        async for m in users.find({"is_master_admin": True}, {"email": 1}):
            if m.get("email"):
                await send_templated_email(template, m["email"], variables)
    except Exception as exc:  # noqa: BLE001 - a missed note never stops the change
        logger.warning("Team note %s was not sent: %s", template, exc)


# ── what I may do ────────────────────────────────────────────────────────

@router.get("/me")
@limiter.limit("120/minute")
async def me(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """The signed-in person's Ops access, for the menu and pages. Someone who is not staff gets level None."""
    access = await ST.access_for(user)
    return {
        "level": access.get("level"), "level_label": ST.LEVEL_LABEL.get(access.get("level") or "", ""),
        "role_name": access.get("role_name"), "designation": access.get("designation"),
        "areas": access.get("areas") or {},
    }


@router.get("/areas")
@limiter.limit("60/minute")
async def areas(request: Request, ctx=Depends(_team_access)) -> dict:
    return {"areas": [{"key": k, "label": v["label"], "soon": bool(v.get("soon"))} for k, v in ST.AREAS.items()],
            "actions": list(ST.ACTIONS)}


# ── staff ────────────────────────────────────────────────────────────────

async def _staff_rows() -> list[dict]:
    await ST.ensure_templates()
    roles = {r["id"]: r["name"] async for r in ST.roles_col().find({}, {"id": 1, "name": 1})}
    rows = []
    async for u in users.find({"$or": [{"is_platform_staff": True}, {"is_master_admin": True}, {"staff": {"$exists": True}}]},
                              {"id": 1, "name": 1, "email": 1, "is_master_admin": 1, "is_platform_staff": 1, "staff": 1, "last_login_at": 1}):
        level = _level_of(u)
        if not level:
            continue
        s = u.get("staff") or {}
        rows.append({
            "id": u["id"], "name": u.get("name") or "", "email": u.get("email") or "", "level": level,
            "level_label": ST.LEVEL_LABEL[level], "role_id": s.get("role_id"), "role_name": roles.get(s.get("role_id")),
            "designation": s.get("designation") or "", "status": s.get("status") or "active",
            "exceptions": ST.clean_permissions(s.get("exceptions")), "joined_at": s.get("joined_at"), "invited_by": s.get("invited_by_name"),
        })
    order = {"master_admin": 0, "admin": 1, "staff": 2}
    rows.sort(key=lambda r: (r["status"] != "active", order[r["level"]], r["name"].lower()))
    return rows


@router.get("/staff")
@limiter.limit("60/minute")
async def list_staff(request: Request, ctx=Depends(_team_access)) -> dict:
    return {"staff": await _staff_rows()}


@router.get("/staff/export.csv")
@limiter.limit("10/minute")
async def export_staff(request: Request, ctx=Depends(_team_access)) -> Response:
    user, access = ctx
    if not ST.can(access, "team", "approve"):
        raise HTTPException(status_code=403, detail="Exporting the team needs Approve on Team. Ask your Master Admin.")
    rows = await _staff_rows()
    await ST.audit(user, "export", "team", after={"what": "team", "rows": len(rows)})
    return csv_response("ekavo-team", ["Name", "Email", "Level", "Role", "Designation", "Status", "Joined", "Invited by"],
                        [[r["name"], r["email"], r["level_label"], r["role_name"] or "", r["designation"], r["status"], r["joined_at"], r["invited_by"] or ""]
                         for r in rows])


class StaffPatch(BaseModel):
    designation: Optional[str] = Field(None, max_length=60)
    level: Optional[Literal["master_admin", "admin", "staff"]] = None
    role_id: Optional[str] = Field(None, max_length=60)
    exceptions: Optional[dict[str, list[str]]] = None
    reason: str = Field(..., min_length=3, max_length=300)


@router.patch("/staff/{user_id}")
@limiter.limit("30/minute")
async def update_staff(request: Request, user_id: str, body: StaffPatch, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    target = await users.find_one({"id": user_id})
    level = _level_of(target or {})
    if not target or not level:
        raise HTTPException(status_code=404, detail="That person isn't on the team.")
    if user_id == me_user["id"] and (body.level or body.role_id or body.exceptions is not None):
        raise HTTPException(status_code=403, detail="You can't change your own access.")
    _may_manage(access, level)
    new_level = body.level or level
    if body.level and body.level != level:
        _master(access)
        if level == "master_admin" and await _masters_count(exclude=user_id) == 0:
            raise HTTPException(status_code=400, detail="There must always be a Master Admin. Make someone else Master Admin first.")
    if body.exceptions is not None:
        _master(access)
    role_id = body.role_id if body.role_id is not None else (target.get("staff") or {}).get("role_id")
    if new_level == "staff":
        if not await _role_is_on(role_id):
            raise HTTPException(status_code=400, detail="Choose a role that is switched on.")
    before = {"level": level, **{k: (target.get("staff") or {}).get(k) for k in ("role_id", "designation", "exceptions")}}
    changes: dict[str, Any] = {"staff.level": new_level, "staff.role_id": role_id if new_level == "staff" else None,
                               "staff.updated_at": _now()}
    if body.designation is not None:
        changes["staff.designation"] = body.designation.strip()
    if body.exceptions is not None:
        changes["staff.exceptions"] = ST.clean_permissions(body.exceptions)
    if (target.get("staff") or {}).get("status") != "deactivated":
        changes["is_master_admin"] = new_level == "master_admin"
        changes["is_platform_staff"] = True
    await users.update_one({"id": user_id}, {"$set": changes})
    await ST.sync_module_roles(user_id)
    after = {k.split(".", 1)[-1]: v for k, v in changes.items() if k.startswith("staff.") and k != "staff.updated_at"}
    await ST.audit(me_user, "staff.changed", user_id, before=before, after=after, reason=body.reason)
    if new_level != level:
        await _tell_masters("staff-access-changed", {"NAME": target.get("name") or target.get("email"),
                                                     "LEVEL": ST.LEVEL_LABEL[new_level], "BY": me_user.get("name") or me_user.get("email")})
    return {"ok": True}


class ReasonIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/staff/{user_id}/deactivate")
@limiter.limit("20/minute")
async def deactivate(request: Request, user_id: str, body: ReasonIn, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    target = await users.find_one({"id": user_id})
    level = _level_of(target or {})
    if not target or not level or (target.get("staff") or {}).get("status") == "deactivated":
        raise HTTPException(status_code=404, detail="That person isn't an active team member.")
    if user_id == me_user["id"]:
        raise HTTPException(status_code=403, detail="You can't deactivate yourself.")
    _may_manage(access, level)
    if level == "master_admin" and await _masters_count(exclude=user_id) == 0:
        raise HTTPException(status_code=400, detail="There must always be a Master Admin.")
    await users.update_one({"id": user_id}, {"$set": {
        "staff.level": level, "staff.status": "deactivated", "staff.deactivated_at": _now(), "staff.deactivated_by": me_user["id"],
        "is_platform_staff": False, "is_master_admin": False,
    }})
    await users.update_one({"id": user_id}, {"$unset": {"support_role": ""}})
    ST.forget(user_id)
    await ST.audit(me_user, "staff.deactivated", user_id, before={"level": level}, reason=body.reason)
    return {"ok": True}


@router.post("/staff/{user_id}/reactivate")
@limiter.limit("20/minute")
async def reactivate(request: Request, user_id: str, body: ReasonIn, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    _master(access)
    target = await users.find_one({"id": user_id})
    s = (target or {}).get("staff") or {}
    if not target or s.get("status") != "deactivated":
        raise HTTPException(status_code=404, detail="That person isn't deactivated.")
    level = s.get("level") or "staff"
    await users.update_one({"id": user_id}, {"$set": {
        "staff.status": "active", "staff.reactivated_at": _now(), "is_platform_staff": True, "is_master_admin": level == "master_admin",
    }})
    await ST.sync_module_roles(user_id)
    await ST.audit(me_user, "staff.reactivated", user_id, after={"level": level}, reason=body.reason)
    return {"ok": True}


# ── invites ──────────────────────────────────────────────────────────────

class InviteIn(BaseModel):
    email: str = Field(..., max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = Field(..., min_length=1, max_length=80)
    designation: str = Field("", max_length=60)
    level: Literal["master_admin", "admin", "staff"] = "staff"
    role_id: Optional[str] = Field(None, max_length=60)


def _invite_link(token: str) -> str:
    return f"{(settings.FRONTEND_URL or '').rstrip('/')}/staff-invite/{token}"


async def _send_invite(doc: dict, inviter: dict) -> None:
    from app.core.notifications import send_templated_email

    role = await ST.roles_col().find_one({"id": doc.get("role_id")}, {"name": 1}) if doc.get("role_id") else None
    try:
        await send_templated_email("staff-invite", doc["email"], {
            "NAME": doc["name"], "INVITED_BY": inviter.get("name") or inviter.get("email") or "Ekavo",
            "ROLE": (role or {}).get("name") or ST.LEVEL_LABEL[doc["level"]], "DESIGNATION": doc.get("designation") or "",
            "LINK": _invite_link(doc["token"]), "DAYS": INVITE_DAYS,
        })
    except Exception as exc:  # noqa: BLE001 - the link can also be copied from Ops
        logger.warning("Staff invite email to %s was not sent: %s", doc["email"], exc)


@router.post("/invites")
@limiter.limit("20/minute")
async def invite(request: Request, body: InviteIn, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    _may_manage(access, body.level)
    if body.level in ("master_admin", "admin"):
        _master(access)
    if body.level == "staff" and not await _role_is_on(body.role_id):
        raise HTTPException(status_code=400, detail="Choose a role that is switched on.")
    email = str(body.email).strip().lower()
    existing = await users.find_one({"email": email}, {"id": 1, "is_platform_staff": 1, "is_master_admin": 1, "staff": 1})
    if existing and _level_of(existing) and (existing.get("staff") or {}).get("status") != "deactivated":
        raise HTTPException(status_code=409, detail="This person is already on the team.")
    await ST.invites_col().update_many({"email": email, "status": "pending"}, {"$set": {"status": "replaced", "updated_at": _now()}})
    doc = {
        "id": secrets.token_hex(8), "token": secrets.token_urlsafe(24), "email": email, "name": body.name.strip(),
        "designation": body.designation.strip(), "level": body.level, "role_id": body.role_id if body.level == "staff" else None,
        "status": "pending", "invited_by": me_user["id"], "invited_by_name": me_user.get("name") or me_user.get("email"),
        "created_at": _now(), "expires_at": _now() + timedelta(days=INVITE_DAYS),
    }
    await ST.invites_col().insert_one(dict(doc))
    await _send_invite(doc, me_user)
    await ST.audit(me_user, "invite.sent", email, after={"level": body.level, "role_id": doc["role_id"], "designation": doc["designation"]})
    return {"invite": _public_invite(doc), "link": _invite_link(doc["token"])}


def _public_invite(doc: dict) -> dict:
    expires = doc.get("expires_at")
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    status = doc.get("status")
    if status == "pending" and expires and expires <= _now():
        status = "expired"
    return {k: doc.get(k) for k in ("id", "email", "name", "designation", "level", "role_id", "invited_by_name", "created_at")} | {
        "status": status, "expires_at": expires, "level_label": ST.LEVEL_LABEL.get(doc.get("level"), ""),
    }


@router.get("/invites")
@limiter.limit("60/minute")
async def list_invites(request: Request, ctx=Depends(_team_access)) -> dict:
    rows = await ST.invites_col().find({"status": "pending"}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return {"invites": [_public_invite(r) for r in rows]}


@router.post("/invites/{invite_id}/resend")
@limiter.limit("10/minute")
async def resend_invite(request: Request, invite_id: str, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    doc = await ST.invites_col().find_one({"id": invite_id, "status": "pending"})
    if not doc:
        raise HTTPException(status_code=404, detail="That invite is no longer open.")
    _may_manage(access, doc["level"])
    fresh = {"token": secrets.token_urlsafe(24), "expires_at": _now() + timedelta(days=INVITE_DAYS), "updated_at": _now()}
    await ST.invites_col().update_one({"id": invite_id}, {"$set": fresh})
    doc.update(fresh)
    await _send_invite(doc, me_user)
    return {"invite": _public_invite(doc), "link": _invite_link(doc["token"])}


@router.delete("/invites/{invite_id}")
@limiter.limit("20/minute")
async def cancel_invite(request: Request, invite_id: str, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    doc = await ST.invites_col().find_one({"id": invite_id, "status": "pending"})
    if not doc:
        raise HTTPException(status_code=404, detail="That invite is no longer open.")
    _may_manage(access, doc["level"])
    await ST.invites_col().update_one({"id": invite_id}, {"$set": {"status": "cancelled", "updated_at": _now()}})
    await ST.audit(me_user, "invite.cancelled", doc["email"])
    return {"ok": True}


async def _open_invite(token: str) -> dict:
    doc = await ST.invites_col().find_one({"token": token})
    if not doc:
        raise HTTPException(status_code=404, detail="This invite link isn't valid.")
    shown = _public_invite(doc)
    if shown["status"] != "pending":
        raise HTTPException(status_code=410, detail="This invite has expired or was already used. Ask for a new one.")
    return doc


@router.get("/invite/{token}")
@limiter.limit("30/minute")
async def view_invite(request: Request, token: str, user: dict = Depends(get_current_user)) -> dict:
    """The invite as the invited person sees it, signed in."""
    doc = await _open_invite(token[:100])
    role = await ST.roles_col().find_one({"id": doc.get("role_id")}, {"name": 1, "description": 1}) if doc.get("role_id") else None
    return {
        "name": doc["name"], "designation": doc.get("designation") or "", "invited_by": doc.get("invited_by_name"),
        "level_label": ST.LEVEL_LABEL[doc["level"]], "role_name": (role or {}).get("name"), "role_description": (role or {}).get("description"),
        "email_matches": (user.get("email") or "").strip().lower() == doc["email"], "invited_email": doc["email"],
    }


@router.post("/invite/{token}/accept")
@limiter.limit("10/minute")
async def accept_invite(request: Request, token: str, user: dict = Depends(get_current_user)) -> dict:
    doc = await _open_invite(token[:100])
    if (user.get("email") or "").strip().lower() != doc["email"]:
        raise HTTPException(status_code=403, detail=f"This invite is for {doc['email']}. Sign in with that email to accept it.")
    claimed = await ST.invites_col().find_one_and_update({"id": doc["id"], "status": "pending"}, {"$set": {"status": "accepted", "accepted_at": _now()}})
    if not claimed:
        raise HTTPException(status_code=410, detail="This invite was already used.")
    await users.update_one({"id": user["id"]}, {"$set": {
        "is_platform_staff": True, "is_master_admin": doc["level"] == "master_admin",
        "staff": {"level": doc["level"], "role_id": doc.get("role_id"), "designation": doc.get("designation") or "", "status": "active",
                  "invited_by": doc.get("invited_by"), "invited_by_name": doc.get("invited_by_name"), "joined_at": _now(), "exceptions": {}},
    }})
    await ST.sync_module_roles(user["id"])
    await ST.audit(user, "invite.accepted", user["id"], after={"level": doc["level"], "role_id": doc.get("role_id")})
    await _tell_masters("staff-joined", {"NAME": user.get("name") or user.get("email"), "LEVEL": ST.LEVEL_LABEL[doc["level"]],
                                         "BY": doc.get("invited_by_name") or ""})
    return {"ok": True}


# ── roles and the permission grid ────────────────────────────────────────

@router.get("/roles")
@limiter.limit("60/minute")
async def list_roles(request: Request, ctx=Depends(_team_access)) -> dict:
    await ST.ensure_templates()
    counts: dict[str, int] = {}
    async for u in users.find({"staff.role_id": {"$exists": True}, "staff.status": {"$ne": "deactivated"}, "staff.level": "staff"}, {"staff.role_id": 1}):
        rid = (u.get("staff") or {}).get("role_id")
        counts[rid] = counts.get(rid, 0) + 1
    rows = await ST.roles_col().find({}, {"_id": 0}).to_list(200)
    order = [t["key"] for t in ST.TEMPLATES]
    rows.sort(key=lambda r: (order.index(r["id"]) if r["id"] in order else 999, r["name"].lower()))
    return {"roles": [{**r, "members": counts.get(r["id"], 0)} for r in rows]}


class RoleIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=40)
    description: str = Field("", max_length=200)
    group: str = Field("Custom", max_length=40)
    permissions: dict[str, list[str]] = {}
    active: bool = True
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/roles")
@limiter.limit("20/minute")
async def create_role(request: Request, body: RoleIn, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    _master(access)
    if await ST.roles_col().find_one({"name": {"$regex": f"^{_esc(body.name.strip())}$", "$options": "i"}}):
        raise HTTPException(status_code=409, detail="A role with that name already exists.")
    doc = {"id": secrets.token_hex(6), "key": None, "name": body.name.strip(), "group": body.group.strip() or "Custom",
           "description": body.description.strip(), "permissions": ST.clean_permissions(body.permissions), "active": body.active,
           "template": False, "created_at": _now(), "updated_at": _now(), "updated_by": me_user["id"]}
    doc["permissions"].pop("team", None)
    await ST.roles_col().insert_one(dict(doc))
    ST.forget()
    await ST.audit(me_user, "role.created", doc["id"], after={"name": doc["name"], "permissions": doc["permissions"]}, reason=body.reason)
    return {"role": {k: v for k, v in doc.items() if k != "_id"}}


def _esc(text: str) -> str:
    import re

    return re.escape(text)


class RolePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=40)
    description: Optional[str] = Field(None, max_length=200)
    permissions: Optional[dict[str, list[str]]] = None
    active: Optional[bool] = None
    reason: str = Field(..., min_length=3, max_length=300)


@router.patch("/roles/{role_id}")
@limiter.limit("30/minute")
async def update_role(request: Request, role_id: str, body: RolePatch, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    _master(access)
    role = await ST.roles_col().find_one({"id": role_id})
    if not role:
        raise HTTPException(status_code=404, detail="That role doesn't exist.")
    changes: dict[str, Any] = {"updated_at": _now(), "updated_by": me_user["id"]}
    if body.name is not None:
        changes["name"] = body.name.strip()
    if body.description is not None:
        changes["description"] = body.description.strip()
    if body.permissions is not None:
        perms = ST.clean_permissions(body.permissions)
        perms.pop("team", None)  # roles below Admin never manage the team
        changes["permissions"] = perms
    if body.active is False and await users.count_documents({"staff.role_id": role_id, "staff.status": {"$ne": "deactivated"}, "staff.level": "staff"}):
        raise HTTPException(status_code=400, detail="People still have this role. Give them another role first.")
    if body.active is not None:
        changes["active"] = body.active
    await ST.roles_col().update_one({"id": role_id}, {"$set": changes})
    ST.forget()
    await ST.sync_role_members(role_id)
    await ST.audit(me_user, "role.changed", role_id, before={k: role.get(k) for k in ("name", "permissions", "active")},
                   after={k: v for k, v in changes.items() if k not in ("updated_at", "updated_by")}, reason=body.reason)
    return {"ok": True}


@router.delete("/roles/{role_id}")
@limiter.limit("20/minute")
async def delete_role(request: Request, role_id: str, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    _master(access)
    role = await ST.roles_col().find_one({"id": role_id})
    if not role:
        raise HTTPException(status_code=404, detail="That role doesn't exist.")
    if await users.count_documents({"staff.role_id": role_id}):
        raise HTTPException(status_code=400, detail="Someone has or had this role. Switch it off instead, so the history stays clear.")
    await ST.roles_col().delete_one({"id": role_id})
    if role.get("template"):
        # a starting role taken out on purpose is not written back
        await ST.settings_col().update_one({"_id": "templates"}, {"$addToSet": {"removed": role_id}}, upsert=True)
    ST.forget()
    await ST.audit(me_user, "role.deleted", role_id, before={"name": role.get("name")})
    return {"ok": True}


# ── settings and history ─────────────────────────────────────────────────

@router.get("/settings")
@limiter.limit("60/minute")
async def get_team_settings(request: Request, ctx=Depends(_team_access)) -> dict:
    return await ST.get_settings()


class SettingsIn(BaseModel):
    admins_may_invite: bool
    reason: str = Field(..., min_length=3, max_length=300)


@router.put("/settings")
@limiter.limit("20/minute")
async def put_team_settings(request: Request, body: SettingsIn, ctx=Depends(_team_access)) -> dict:
    me_user, access = ctx
    _master(access)
    before = await ST.get_settings()
    await ST.settings_col().update_one({"_id": "team"}, {"$set": {"admins_may_invite": body.admins_may_invite, "updated_at": _now()}}, upsert=True)
    ST.forget()
    await ST.audit(me_user, "team.settings", "team", before=before, after={"admins_may_invite": body.admins_may_invite}, reason=body.reason)
    return await ST.get_settings()


HISTORY_PAGE = 50
HISTORY_EXPORT_MAX = 10_000


def _history_query(q: Optional[str], action: Optional[str]) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if q and q.strip():
        term = re.escape(q.strip())
        query["$or"] = [{"by_email": {"$regex": term, "$options": "i"}}, {"target": {"$regex": term, "$options": "i"}},
                        {"reason": {"$regex": term, "$options": "i"}}]
    if action:
        # a group such as "invite" matches invite.sent, invite.accepted and the rest
        query["action"] = {"$regex": rf"^{re.escape(action)}(\.|$)"}
    return query


async def _history_rows(query: dict[str, Any], *, skip: int, limit: int) -> list[dict]:
    rows = await ST.audit_col().find(query, {"_id": 0}).sort("at", -1).skip(skip).limit(limit).to_list(limit)
    from app.shared.audit_names import readable

    return await readable([{**r, "target_name": None} for r in rows])


@router.get("/history")
@limiter.limit("60/minute")
async def history(request: Request, ctx=Depends(_team_access), q: Optional[str] = Query(None, max_length=100),
                  action: Optional[str] = Query(None, max_length=40, pattern=r"^[a-z_.]+$"), page: int = Query(0, ge=0, le=500)) -> dict:
    _, access = ctx
    _master(access)
    rows = await _history_rows(_history_query(q, action), skip=page * HISTORY_PAGE, limit=HISTORY_PAGE)
    return {"history": rows, "page": page, "more": len(rows) == HISTORY_PAGE}


LEDGER_DAYS = 30


@router.get("/ledger")
@limiter.limit("30/minute")
async def staff_ledger(request: Request, ctx=Depends(_team_access)) -> dict:
    """The staff ledger: for each person who works in Ops, what they did in the last 30 days, when they last did anything, and the
    kinds of change they made. The full line-by-line list is the History tab."""
    _, access = ctx
    _master(access)
    since = _now() - timedelta(days=LEDGER_DAYS)
    rows = await ST.audit_col().aggregate([
        {"$match": {"at": {"$gte": since}, "by_email": {"$nin": [None, ""]}}},
        {"$group": {"_id": {"email": "$by_email", "action": "$action"}, "n": {"$sum": 1}, "last": {"$max": "$at"}}},
    ]).to_list(5000)
    people: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = people.setdefault(r["_id"]["email"], {"email": r["_id"]["email"], "actions": 0, "last_at": None, "kinds": {}})
        p["actions"] += r["n"]
        p["kinds"][r["_id"]["action"].split(".")[0]] = p["kinds"].get(r["_id"]["action"].split(".")[0], 0) + r["n"]
        if p["last_at"] is None or r["last"] > p["last_at"]:
            p["last_at"] = r["last"]
    staff = {u.get("email"): u async for u in users.find({"$or": [{"is_platform_staff": True}, {"is_master_admin": True}, {"staff.level": {"$exists": True}}]},
                                                         {"_id": 0, "id": 1, "name": 1, "email": 1, "staff": 1, "is_master_admin": 1})}
    out = []
    for email in sorted(set(people) | set(staff)):
        p = people.get(email, {"actions": 0, "last_at": None, "kinds": {}})
        u = staff.get(email) or {}
        s = u.get("staff") or {}
        out.append({
            "email": email, "name": u.get("name") or "", "level": "master_admin" if u.get("is_master_admin") else s.get("level") or "",
            "status": s.get("status") or ("active" if u else "former"), "designation": s.get("designation") or "",
            "actions": p["actions"], "last_at": p["last_at"].isoformat() if p["last_at"] else None,
            "kinds": dict(sorted(p["kinds"].items(), key=lambda kv: -kv[1])[:4]),
        })
    out.sort(key=lambda r: (-r["actions"], r["email"]))
    return {"days": LEDGER_DAYS, "people": out}


@router.get("/history/export.csv")
@limiter.limit("10/minute")
async def export_history(request: Request, ctx=Depends(_team_access), q: Optional[str] = Query(None, max_length=100),
                         action: Optional[str] = Query(None, max_length=40, pattern=r"^[a-z_.]+$")) -> Response:
    user, access = ctx
    _master(access)
    rows = await _history_rows(_history_query(q, action), skip=0, limit=HISTORY_EXPORT_MAX)
    await ST.audit(user, "export", "team", after={"what": "history", "rows": len(rows)})
    return csv_response("ekavo-team-history", ["When", "Who", "What", "About", "Reason"],
                        [[r.get("at"), r.get("by_email"), r.get("action"), r.get("target_name"), r.get("reason") or ""] for r in rows])
