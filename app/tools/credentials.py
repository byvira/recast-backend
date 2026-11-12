"""Keys for the tool layer. They live in the same `api_keys` collection as the video API keys, so one key list, one revoke and one plan limit
cover both; a key made here also carries the tool scopes, capabilities, a monthly credit cap and an expiry. A key made before this had no tool
scopes, so it cannot call a tool until a new one is made.

A key is a workspace service key: it acts as the member who made it, so removing or demoting that member takes effect on the next call
(see gatekeeper: the person's current role is read on every call). The secret is shown once and only its hash is kept."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4

from app.tools.contract import Scope
from app.tools.gatekeeper import CAP_BRAND_IDENTITY, CAP_DELETE, Credential
from app.video import api_keys

#: what an owner may switch on for one named key, beyond its scopes
CAPABILITIES = (CAP_DELETE, CAP_BRAND_IDENTITY)
DEFAULT_DAYS = 90
MAX_DAYS = 365
DEFAULT_CREDIT_CAP = 100
MAX_CREDIT_CAP = 1_000_000


class CredentialError(ValueError):
    """A refusal with a plain message."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def create_key(
    workspace_id: str, user_id: str, name: str, scopes: Iterable[str], *, capabilities: Iterable[str] = (),
    credit_cap: Optional[int] = DEFAULT_CREDIT_CAP, days: int = DEFAULT_DAYS, allow_approve: bool = False,
) -> tuple[dict[str, Any], str]:
    """Make a key for the tools. Returns (its public record, the key itself, shown once).

    A key can read, create or publish. It can approve (a person's decision) only when the owner names it and sets `allow_approve`, and admin
    is never given to a key."""
    name = (name or "").strip()[:60]
    if not name:
        raise CredentialError("Give the key a name so you can tell it apart later.")
    chosen: list[str] = []
    for raw in dict.fromkeys(scopes or ()):
        try:
            scope = Scope(raw)
        except ValueError:
            raise CredentialError(f"{raw!r} is not something a key can be allowed to do.")
        if scope is Scope.ADMIN:
            raise CredentialError("A key can never manage keys, people or settings. That stays with a person in Ekavo.")
        if scope is Scope.APPROVE and not allow_approve:
            raise CredentialError("Approving is a person's decision. The owner has to switch that on for this one key by name.")
        chosen.append(scope.value)
    if not chosen:
        raise CredentialError("Choose what the key may do: read, create or publish.")
    extra = [c for c in dict.fromkeys(capabilities or ())]
    unknown = [c for c in extra if c not in CAPABILITIES]
    if unknown:
        raise CredentialError(f"{unknown[0]!r} is not something a key can be given.")
    if credit_cap is not None and not (0 < int(credit_cap) <= MAX_CREDIT_CAP):
        raise CredentialError(f"The monthly credit cap is a number from 1 to {MAX_CREDIT_CAP:,}.")
    if not (1 <= int(days) <= MAX_DAYS):
        raise CredentialError(f"A key lasts from 1 to {MAX_DAYS} days.")

    from app.shared import entitlements

    await entitlements.require_room(
        await entitlements.entitlements_for(workspace_id), "api_keys",
        legacy_message=f"A workspace can have {api_keys.MAX_KEYS} keys. Revoke one you no longer use first.",
    )
    plain = api_keys.PREFIX + secrets.token_urlsafe(30)
    now = _now()
    doc = {
        "id": uuid4().hex, "workspace_id": workspace_id, "name": name, "prefix": plain[: len(api_keys.PREFIX) + 6], "hash": api_keys._hash(plain),
        "scopes": [], "tool_scopes": chosen, "capabilities": extra, "credit_cap": int(credit_cap) if credit_cap is not None else None,
        "expires_at": now + timedelta(days=int(days)), "created_by": user_id, "created_at": now, "last_used_at": None, "revoked": False,
    }
    await api_keys._keys().insert_one(dict(doc))
    return {**api_keys.public(doc), "tool_scopes": chosen, "capabilities": extra, "credit_cap": doc["credit_cap"],
            "expires_at": api_keys._iso(doc["expires_at"])}, plain


def to_credential(doc: dict[str, Any]) -> Credential:
    scopes = frozenset(Scope(s) for s in (doc.get("tool_scopes") or ()) if s in {x.value for x in Scope})
    return Credential(
        id=doc["id"], surface=doc.get("surface") or "api", workspace_id=doc["workspace_id"], user_id=doc.get("created_by"), scopes=scopes,
        capabilities=frozenset(doc.get("capabilities") or ()), credit_cap=doc.get("credit_cap"), revoked=bool(doc.get("revoked")),
        expires_at=_aware(doc.get("expires_at")),
    )


async def load(credential_id: str) -> Optional[Credential]:
    """The credential as it is now. Read on every call, so a revoke acts at once."""
    doc = await api_keys._keys().find_one({"id": credential_id})
    return to_credential(doc) if doc else None


async def credential_for_secret(plain: str) -> Optional[str]:
    """The id of the key this secret belongs to, or None. A revoked, expired or unknown key all give the same None, and the last-used time
    is written at most every ten minutes."""
    doc = await api_keys.authenticate(plain)
    return doc["id"] if doc else None


async def list_tool_keys(workspace_id: str) -> list[dict[str, Any]]:
    """The workspace's tool keys with what each may do, newest first. Never the secret or the hash."""
    from app.tools import ledger

    rows = await api_keys._keys().find({"workspace_id": workspace_id, "tool_scopes": {"$exists": True}, "surface": {"$exists": False}}).sort("created_at", -1).to_list(length=50)
    return [{**api_keys.public(r), "tool_scopes": r.get("tool_scopes") or [], "capabilities": r.get("capabilities") or [],
             "credit_cap": r.get("credit_cap"), "expires_at": api_keys._iso(r.get("expires_at")),
             "credits_this_month": 0 if r.get("revoked") else await ledger.spent_this_month(r["id"])} for r in rows]


async def usage(workspace_id: str, key_id: str, *, days: int = 30) -> Optional[dict[str, Any]]:
    """What one key did lately: calls by tool and outcome, and its credits this month against its cap."""
    from app.tools import ledger

    doc = await api_keys._keys().find_one({"id": key_id, "workspace_id": workspace_id})
    if not doc:
        return None
    since = _now() - timedelta(days=days)
    rows = await ledger.calls().aggregate([
        {"$match": {"credential_id": key_id, "at": {"$gte": since}}},
        {"$group": {"_id": {"tool": "$tool", "outcome": "$outcome"}, "calls": {"$sum": 1}}},
        {"$sort": {"calls": -1}}, {"$limit": 100},
    ]).to_list(length=100)
    return {
        "key_id": key_id, "days": days, "credits_this_month": await ledger.spent_this_month(key_id), "credit_cap": doc.get("credit_cap"),
        "calls": [{"tool": r["_id"]["tool"], "outcome": r["_id"]["outcome"], "count": r["calls"]} for r in rows],
    }
