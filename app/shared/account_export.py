"""A copy of a person's own account data, for the member who asks for it.

It holds the account details, the workspaces they belong to and their role in each, their personal settings, and their support history.
It never holds sign-in secrets, key values or anything that belongs to another person. Pictures, audio, video and other files are not included.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.mongo import get_db, users, workspace_members, workspaces
from app.shared.account_erase import MEMBER_ROWS

#: A field whose name contains one of these is never exported, whatever it is called.
_SECRET_PARTS = ("secret", "hash", "password", "token", "totp", "otp", "recovery", "key", "endpoint", "auth")
#: Fields that are only storage bookkeeping.
_SKIP = {"_id"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in _SKIP and not any(p in k.lower() for p in _SECRET_PARTS)}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def export_account(user_id: str) -> dict[str, Any]:
    user = await users.find_one({"id": user_id}) or {}
    memberships = [m async for m in workspace_members.find({"user_id": user_id})]
    ids = [m["workspace_id"] for m in memberships if m.get("workspace_id")]
    named = {w["id"]: w async for w in workspaces.find({"id": {"$in": ids}}, {"id": 1, "name": 1, "owner_id": 1})}

    db = get_db()
    personal: dict[str, list[Any]] = {}
    for name in MEMBER_ROWS:
        if name == "push_subscriptions":
            continue  # device addresses, not the member's words
        rows = [_clean(r) async for r in db[name].find({"user_id": user_id}).limit(1000)]
        if rows:
            personal[name] = rows

    from app.shared import support_privacy

    try:
        support = _clean(await support_privacy.export_member_data(user_id))
    except Exception:  # noqa: BLE001 - the rest of the copy is still useful
        support = None

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": _clean(user),
        "workspaces": [
            {
                "workspace_id": m.get("workspace_id"),
                "name": named.get(m.get("workspace_id"), {}).get("name"),
                "role": m.get("role"),
                "status": m.get("status"),
                "owner": named.get(m.get("workspace_id"), {}).get("owner_id") == user_id,
                "joined_at": _clean(m.get("joined_at") or m.get("created_at")),
            }
            for m in memberships
        ],
        "personal_settings": personal,
        "support": support,
        "not_included": "Pictures, audio, video and other files, and posts made in workspaces, are not part of this copy.",
    }
