"""Deleting a person's account and their data for good, when they ask for it.

What goes: every workspace they own (its stored files and every record keyed by it, the same as a purged workspace), their place in
other workspaces, invites sent to their email, their personal settings and support history, their sign-in sessions, and the account.
What stays: receipts and payment records (tax law keeps them for years), and what they made inside someone else's workspace, which
belongs to that workspace. Ekavo staff are taken off the Ops team first, and a paid plan that still renews is cancelled first.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db.mongo import get_db, invites, users, workspace_members, workspaces

logger = logging.getLogger(__name__)

#: Collections kept untouched: records the law or the books need, Ops' own history, and shared data that belongs to nobody.
KEEP = {"billing_invoices", "billing_payments", "billing_audit", "staff_audit", "migrations", "localized_strings", "account_erasures"}
#: Personal rows that live inside a workspace but belong to one member (their voice settings, word lists, read state, devices).
MEMBER_ROWS = ("member_voice_settings", "member_lexicon", "inbox_state", "push_subscriptions", "notification_prefs")


async def erase_account(user: dict[str, Any]) -> dict[str, Any]:
    uid = user["id"]
    if user.get("is_platform_staff") or user.get("is_master_admin"):
        raise HTTPException(status_code=409, detail="This account is on the Ekavo team. Take it off the team in Ops first.")
    owned = [w async for w in workspaces.find({"owner_id": uid}, {"id": 1, "name": 1})]
    from app.billing import subscriptions as billing_subs

    for w in owned:
        if await billing_subs.renewing(w["id"]):
            raise HTTPException(status_code=409, detail=f"\"{w.get('name') or 'A workspace'}\" has a paid plan that renews. Cancel it in Billing first.")

    from app.workers.workspace_purge import purge_workspace

    removed: dict[str, int] = {}
    for w in owned:
        for name, count in (await purge_workspace(w["id"])).items():
            removed[name] = removed.get(name, 0) + count

    from app.shared import support_privacy

    try:
        await support_privacy.erase_member_data(uid)
    except Exception as exc:  # noqa: BLE001 - the rest still goes; Ops sees the log
        logger.error("support data for %s could not be erased: %s", uid, exc)

    db = get_db()
    names = set(await db.list_collection_names())
    for name in names - KEEP - {"users", "workspaces"}:
        # rows that belong to the person and to no workspace (sessions, devices, personal history), and their own rows inside a workspace
        flt: dict[str, Any] = {"$or": [{"user_id": uid, "workspace_id": {"$exists": False}}, {"owner_user_id": uid, "workspace_id": {"$exists": False}}]}
        if name in MEMBER_ROWS:
            flt = {"user_id": uid}
        result = await db[name].delete_many(flt)
        if result.deleted_count:
            removed[name] = removed.get(name, 0) + result.deleted_count
    await workspace_members.delete_many({"user_id": uid})
    if user.get("email"):
        await invites.delete_many({"email": user["email"]})
    await users.delete_one({"id": uid})

    # a record that an erasure happened, with nothing that identifies the person
    digest = hashlib.sha256(str(user.get("email") or uid).lower().encode()).hexdigest()[:16]
    await db["account_erasures"].insert_one({"at": datetime.now(timezone.utc), "who": digest, "workspaces": len(owned), "rows": sum(removed.values())})
    logger.info("account erased: %s, %d workspaces, %d rows", digest, len(owned), sum(removed.values()))
    return {"workspaces": len(owned), "rows": sum(removed.values())}
