"""The passive half of the Ops desk: what is worth knowing about a workspace or a person, worked out by plain rules.

Nothing here calls a model or costs anything, so it can run every time a card opens. Each signal is one short line with a level:

* ``problem``  something is wrong and a person should act
* ``watch``    worth a look, not urgent
* ``good``     something that is fine and answers a likely question
* ``info``     a plain fact

``workspace_signals`` and ``person_signals`` return the facts they read next to the signals, so the active half (``brief.py``) writes
from the same material and a person can check any line against its facts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import get_db

LEVEL_ORDER = {"problem": 0, "watch": 1, "info": 2, "good": 3}
QUIET_AFTER = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ago(when: Optional[datetime]) -> str:
    if not when:
        return "never"
    days = (_now() - when).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _sorted(signals: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(signals, key=lambda s: LEVEL_ORDER.get(s["level"], 9))


def _add(signals: list[dict[str, str]], level: str, kind: str, text: str) -> None:
    signals.append({"level": level, "kind": kind, "text": text})


async def workspace_signals(workspace_id: str) -> Optional[dict[str, Any]]:
    """The facts about one workspace and the signals they give. None when there is no such workspace."""
    from app.billing import subscriptions as S
    from app.shared import entitlements

    db = get_db()
    ws = await db["workspaces"].find_one({"id": workspace_id[:80]}, {"_id": 0})
    if not ws:
        return None
    wid = ws["id"]
    since_week = _now() - timedelta(days=7)
    since_month = _now() - timedelta(days=30)
    owner = await db["users"].find_one({"id": ws.get("owner_id")}, {"_id": 0, "name": 1, "email": 1}) or {}
    ent = await entitlements.entitlements_for(wid)
    sub = await S.current_for_workspace(wid)
    last_row = await db["activity_entries"].find_one({"workspace_id": wid}, {"occurred_at": 1}, sort=[("occurred_at", -1)])
    last_active = _aware((last_row or {}).get("occurred_at"))

    facts: dict[str, Any] = {
        "name": ws.get("name") or "Unnamed", "owner_name": owner.get("name") or "", "owner_email": owner.get("email") or "",
        "plan": ent.plan, "plan_name": ent.plan_name, "comp": bool(ent.comp), "billing_status": ws.get("billing_status") or "none",
        "created_at": _aware(ws.get("created_at")), "last_active": last_active, "setup_pending": bool(ws.get("setup_pending")),
        "members": await db["workspace_members"].count_documents({"workspace_id": wid, "status": "active"}),
        "brands": await db["brand_profiles"].count_documents({"workspace_id": wid}),
        "connections": await db["workspace_connections"].count_documents({"workspace_id": wid, "is_active": True}),
        "posts_published": await db["content_pieces"].count_documents({"workspace_id": wid, "publish_status": "published"}),
        "posts_failed_week": await db["content_pieces"].count_documents({"workspace_id": wid, "publish_status": "failed", "updated_at": {"$gte": since_week}}),
        "posts_held": await db["content_pieces"].count_documents({"workspace_id": wid, "flagged_for_review": True, "deleted": {"$ne": True}}),
        "safety_open": await db["safety_events"].count_documents({"workspace_id": wid, "status": {"$ne": "reviewed"}}),
        "tickets_open": await db["support_tickets"].count_documents({"workspace_id": wid, "status": {"$nin": ["resolved", "closed"]}}),
        "payments_failed_month": await S.payments_col().count_documents({"workspace_id": wid, "status": "failed", "at": {"$gte": since_month}}),
        "flags_open": await db["ops_workspace_notes"].count_documents({"workspace_id": wid, "flag": True, "open": True}),
        "subscription_status": (sub or {}).get("status"),
    }

    signals: list[dict[str, str]] = []
    status = facts["billing_status"]
    if status == "limited":
        _add(signals, "problem", "billing", "The plan is on hold after a failed payment, so the workspace is limited.")
    elif status == "past_due":
        _add(signals, "problem", "billing", "A renewal payment is failing and is being retried.")
    elif facts["payments_failed_month"]:
        n = facts["payments_failed_month"]
        _add(signals, "watch", "billing", f"{n} payment{'s' if n != 1 else ''} failed in the last 30 days.")
    if status == "active" or (sub or {}).get("status") == "active":
        _add(signals, "good", "billing", f"Paying on the {facts['plan_name']} plan.")
    elif facts["comp"]:
        _add(signals, "info", "billing", f"On the {facts['plan_name']} plan free of charge from Ekavo.")
    else:
        _add(signals, "info", "billing", f"On the {facts['plan_name']} plan.")

    if facts["tickets_open"]:
        n = facts["tickets_open"]
        _add(signals, "watch", "support", f"{n} open support ticket{'s' if n != 1 else ''}.")
    if facts["safety_open"]:
        n = facts["safety_open"]
        _add(signals, "watch", "safety", f"{n} caught item{'s' if n != 1 else ''} waiting for a person to look.")
    if facts["posts_held"]:
        n = facts["posts_held"]
        _add(signals, "watch", "safety", f"{n} post{'s are' if n != 1 else ' is'} held for review.")
    if facts["posts_failed_week"]:
        n = facts["posts_failed_week"]
        _add(signals, "problem" if n >= 3 else "watch", "publishing", f"{n} post{'s' if n != 1 else ''} failed to publish in the last 7 days.")
    if facts["flags_open"]:
        n = facts["flags_open"]
        _add(signals, "watch", "ops", f"{n} open Ops flag{'s' if n != 1 else ''}.")

    if facts["setup_pending"]:
        _add(signals, "watch", "setup", "Setup is not finished yet.")
    elif not facts["brands"]:
        _add(signals, "watch", "setup", "No brand has been set up.")
    elif not facts["connections"]:
        _add(signals, "watch", "setup", "No social accounts are connected.")
    if last_active is None:
        _add(signals, "watch", "activity", "Nothing has happened in this workspace yet.")
    elif _now() - last_active > QUIET_AFTER:
        _add(signals, "watch", "activity", f"Quiet: the last activity was {_ago(last_active)}.")
    else:
        _add(signals, "good", "activity", f"Active: the last activity was {_ago(last_active)}.")
    if not any(s["level"] in ("problem", "watch") for s in signals):
        _add(signals, "good", "overall", "Nothing needs attention.")
    return {"kind": "workspace", "id": wid, "facts": facts, "signals": _sorted(signals)}


async def person_signals(user_id: str) -> Optional[dict[str, Any]]:
    """The facts about one person and the signals they give. None when there is no such person."""
    db = get_db()
    user = await db["users"].find_one({"id": user_id[:80]}, {"_id": 0, "id": 1, "name": 1, "email": 1, "created_at": 1, "is_platform_staff": 1, "is_master_admin": 1})
    if not user:
        return None
    uid = user["id"]
    memberships = [m async for m in db["workspace_members"].find({"user_id": uid, "status": "active"}, {"_id": 0, "workspace_id": 1, "role": 1})]
    names = {w["id"]: w.get("name") or "Unnamed" async for w in db["workspaces"].find({"id": {"$in": [m["workspace_id"] for m in memberships]}}, {"id": 1, "name": 1})}
    tickets_open = await db["support_tickets"].count_documents({"created_by": uid, "status": {"$nin": ["resolved", "closed"]}})
    facts: dict[str, Any] = {
        "name": user.get("name") or "", "email": user.get("email") or "", "joined": _aware(user.get("created_at")),
        "staff": bool(user.get("is_platform_staff") or user.get("is_master_admin")),
        "workspaces": [{"id": m["workspace_id"], "name": names.get(m["workspace_id"], "Unnamed"), "role": m.get("role") or "member"} for m in memberships],
        "tickets_open": tickets_open,
    }
    signals: list[dict[str, str]] = []
    if not memberships:
        _add(signals, "watch", "workspaces", "Not a member of any workspace.")
    else:
        owned = [w for w in facts["workspaces"] if w["role"] == "owner"]
        _add(signals, "info", "workspaces", f"In {len(memberships)} workspace{'s' if len(memberships) != 1 else ''}, owns {len(owned)}.")
    if tickets_open:
        _add(signals, "watch", "support", f"{tickets_open} open support ticket{'s' if tickets_open != 1 else ''}.")
    if facts["staff"]:
        _add(signals, "info", "staff", "Works at Ekavo.")
    if facts["joined"]:
        _add(signals, "info", "joined", f"Joined {_ago(facts['joined'])}.")
    if not any(s["level"] in ("problem", "watch") for s in signals):
        _add(signals, "good", "overall", "Nothing needs attention.")
    return {"kind": "person", "id": uid, "facts": facts, "signals": _sorted(signals)}


def plain_summary(result: dict[str, Any]) -> str:
    """The signals as short lines of plain text. This is what the brief falls back to when the model is not available, so the
    button always gives an answer."""
    lines = [s["text"] for s in result["signals"] if s["level"] != "good" or len(result["signals"]) == 1]
    return " ".join(lines) if lines else "Nothing needs attention."
