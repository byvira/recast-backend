"""Browser sign-in for the command line and for assistants that connect over MCP (Claude, Cursor): no password is ever typed in a terminal.

The flow is the standard device sign-in:
  1. The tool asks for a code (`start`) and shows the person a short code and an address.
  2. The person opens the address in the app, where they are already signed in, checks the name of what is asking, picks a workspace and what it may do,
     and says yes (`approve`) or no (`deny`).
  3. The tool asks again every few seconds (`poll`). The first answer after a yes carries the access token, once. It is made at that moment, so it is
     never stored anywhere in the clear.

A session is a workspace key made for one person on one device or client: it acts as that person with their role (read on every call, so removing or
demoting them acts at once), it is capped to the scopes they chose, it lasts 90 days, and it shows in Settings, Connections where it can be revoked.
It can approve a person's decision only if the person ticked that box at sign-in. It can never manage keys, people or settings."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4

from app.core.rbac import ROLE_PERMISSIONS
from app.db.mongo import get_db
from app.tools.contract import Scope
from app.video import api_keys

CLIENTS = ("cli", "mcp")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"       # no 0, 1, I, L, O: nothing to misread
CODE_LENGTH = 8
CODE_LIFETIME = timedelta(minutes=10)
POLL_EVERY = 3                                          # seconds; a faster poll is told to slow down
SESSION_DAYS = 90
MAX_SESSIONS = 25                                       # sign-ins at once in one workspace (plan numbers per tier are still to set in Ops)
MAX_LOOKUPS = 8                                         # wrong or repeated tries on one code before it is closed

#: what each scope needs from the person's role
NEEDS = {Scope.READ: None, Scope.CREATE: "create_content", Scope.PUBLISH: "publish_content", Scope.APPROVE: "approve_content"}


class SessionError(ValueError):
    """A refusal with a plain message and a stable code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def codes():
    return get_db()["tool_device_codes"]


async def create_indexes() -> None:
    try:
        await codes().create_index("device_hash", unique=True)
        await codes().create_index("user_hash")
        await codes().create_index("expires_at", expireAfterSeconds=3600)
    except Exception:  # noqa: BLE001 - optional: the app starts without them
        pass


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalise(user_code: str) -> str:
    return "".join(ch for ch in (user_code or "").upper() if ch in CODE_ALPHABET)


def pretty(user_code: str) -> str:
    return f"{user_code[:4]}-{user_code[4:]}"


async def start(client: str, label: str, *, address: str, app_url: str) -> dict[str, Any]:
    """Begin a sign-in. Returns the secret `device_code` for the tool to poll with, and the short `user_code` and address for the person."""
    if client not in CLIENTS:
        raise SessionError("input_invalid", "The client is cli or mcp.", 422)
    name = " ".join((label or "").split())[:60] or ("Command line" if client == "cli" else "Assistant app")
    device_code = secrets.token_urlsafe(32)
    user_code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    now = _now()
    await codes().insert_one({
        "id": uuid4().hex, "device_hash": _hash(device_code), "user_hash": _hash(user_code), "client": client, "label": name, "status": "pending",
        "created_at": now, "expires_at": now + CODE_LIFETIME, "lookups": 0, "asked_from": _hash(address)[:16], "last_poll": None,
    })
    return {
        "device_code": device_code, "user_code": pretty(user_code), "verification_url": f"{app_url.rstrip('/')}/device", "verification_url_complete": f"{app_url.rstrip('/')}/device?code={user_code}",
        "expires_in": int(CODE_LIFETIME.total_seconds()), "interval": POLL_EVERY,
    }


async def _pending(user_code: str) -> dict[str, Any]:
    """The waiting sign-in for this code. Looking at it counts as a try, and a code that has been tried too often, or has run out, is closed."""
    wanted = normalise(user_code)
    if len(wanted) != CODE_LENGTH:
        raise SessionError("code_unknown", "That code is not right. Check it against the one on your screen.", 404)
    doc = await codes().find_one({"user_hash": _hash(wanted), "status": "pending"})
    if doc is None or (_aware(doc.get("expires_at")) or _now()) <= _now():
        raise SessionError("code_unknown", "That code is not right or has run out. Start the sign-in again on your device.", 404)
    if int(doc.get("lookups") or 0) >= MAX_LOOKUPS:
        await codes().update_one({"id": doc["id"]}, {"$set": {"status": "closed"}})
        raise SessionError("code_unknown", "That code was tried too many times. Start the sign-in again on your device.", 404)
    await codes().update_one({"id": doc["id"]}, {"$inc": {"lookups": 1}})
    return doc


async def lookup(user_code: str) -> dict[str, Any]:
    """What is asking to sign in, for the person to check before they say yes."""
    doc = await _pending(user_code)
    return {"client": doc["client"], "label": doc["label"], "asked_at": _aware(doc["created_at"]).isoformat().replace("+00:00", "Z")}


async def _allowed_scopes(workspace_id: str, user_id: str, wanted: Iterable[str], allow_approve: bool) -> list[str]:
    member = await get_db()["workspace_members"].find_one({"workspace_id": workspace_id, "user_id": user_id}, {"role": 1, "status": 1})
    if not member or member.get("status", "active") != "active":
        raise SessionError("not_a_member", "You are not an active member of that workspace.", 403)
    held = set(ROLE_PERMISSIONS.get(member.get("role", ""), set()))
    chosen: list[str] = []
    for raw in dict.fromkeys(wanted or ()):
        try:
            scope = Scope(raw)
        except ValueError:
            raise SessionError("input_invalid", f"{raw!r} is not something a sign-in can be allowed to do.", 422)
        if scope is Scope.ADMIN:
            raise SessionError("scope_not_allowed", "A sign-in can never manage keys, people or settings. That stays with you in Ekavo.", 403)
        if scope is Scope.APPROVE and not allow_approve:
            raise SessionError("scope_not_allowed", "Approving is your decision. Tick that box if you want this device to approve for you.", 403)
        need = NEEDS.get(scope)
        if need and need not in held:
            raise SessionError("scope_not_allowed", f"Your role in that workspace cannot {scope.value}, so this sign-in cannot either.", 403)
        chosen.append(scope.value)
    if Scope.READ.value not in chosen:
        chosen.insert(0, Scope.READ.value)
    return chosen


async def ensure_room(workspace_id: str) -> None:
    """Refuse a new sign-in when the workspace has reached its plan's limit (the plan's own message, or the fixed one while plans are off)."""
    from app.shared import entitlements

    try:
        await entitlements.require_room(await entitlements.entitlements_for(workspace_id), "tool_sessions",
                                        legacy_message=f"This workspace has {MAX_SESSIONS} sign-ins already. Revoke one in Settings, Connections first.")
    except Exception as exc:  # noqa: BLE001 - the plan's own message is the one to show
        detail = getattr(exc, "detail", None)
        text = detail.get("message") if isinstance(detail, dict) else detail
        raise SessionError("sessions_full", str(text or getattr(exc, "message", None) or exc), 409) from exc


async def approve(user_code: str, user_id: str, workspace_id: str, scopes: Iterable[str], *, allow_approve: bool = False) -> dict[str, Any]:
    """The person says yes: for this workspace, with these scopes (never more than their own role has)."""
    doc = await _pending(user_code)
    chosen = await _allowed_scopes(workspace_id, user_id, scopes, allow_approve)
    await ensure_room(workspace_id)
    done = await codes().update_one({"id": doc["id"], "status": "pending"}, {"$set": {"status": "approved", "approved_by": user_id, "workspace_id": workspace_id, "scopes": chosen, "approved_at": _now()}})
    if not done.modified_count:
        raise SessionError("code_unknown", "That sign-in was already answered.", 409)
    return {"client": doc["client"], "label": doc["label"], "scopes": chosen}


async def deny(user_code: str) -> None:
    doc = await _pending(user_code)
    await codes().update_one({"id": doc["id"], "status": "pending"}, {"$set": {"status": "denied"}})


async def poll(device_code: str) -> dict[str, Any]:
    """The tool asks whether the person has answered. Pending until they do; the first answer after a yes carries the token, once."""
    now = _now()
    doc = await codes().find_one({"device_hash": _hash(device_code or "")})
    if doc is None:
        raise SessionError("code_unknown", "That sign-in is not known. Start it again.", 404)
    if doc["status"] == "pending":
        if (_aware(doc.get("expires_at")) or now) <= now:
            raise SessionError("expired", "The sign-in ran out. Start it again.", 410)
        last = _aware(doc.get("last_poll"))
        await codes().update_one({"id": doc["id"]}, {"$set": {"last_poll": now}})
        if last and (now - last).total_seconds() < POLL_EVERY - 0.5:
            return {"status": "slow_down", "interval": POLL_EVERY + 2}
        return {"status": "pending", "interval": POLL_EVERY}
    if doc["status"] == "denied":
        raise SessionError("denied", "You said no on the sign-in screen, so nothing was connected.", 403)
    if doc["status"] != "approved":
        raise SessionError("expired", "That sign-in was already used or closed. Start it again.", 410)
    claimed = await codes().update_one({"id": doc["id"], "status": "approved"}, {"$set": {"status": "used", "used_at": now}})
    if not claimed.modified_count:
        raise SessionError("expired", "That sign-in was already used. Start it again.", 410)
    record, token = await _make_session(doc)
    return {"status": "approved", "access_token": token, "token_type": "Bearer", "workspace_id": doc["workspace_id"], "scopes": doc["scopes"], "expires_at": record["expires_at"]}


async def _make_session(doc: dict[str, Any]) -> tuple[dict[str, Any], str]:
    plain = api_keys.PREFIX + secrets.token_urlsafe(30)
    now = _now()
    expires = now + timedelta(days=SESSION_DAYS)
    row = {
        "id": uuid4().hex, "workspace_id": doc["workspace_id"], "name": doc["label"], "prefix": plain[: len(api_keys.PREFIX) + 6], "hash": api_keys._hash(plain),
        "scopes": [], "tool_scopes": list(doc["scopes"]), "capabilities": [], "credit_cap": None, "expires_at": expires, "created_by": doc["approved_by"],
        "created_at": now, "last_used_at": None, "revoked": False, "surface": doc["client"], "client_label": doc["label"],
    }
    await api_keys._keys().insert_one(dict(row))
    return {"id": row["id"], "expires_at": expires.isoformat().replace("+00:00", "Z")}, plain


async def list_sessions(workspace_id: str, user_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The workspace's command line and assistant sign-ins, newest first. Never the token or its hash."""
    from app.tools import ledger

    query: dict[str, Any] = {"workspace_id": workspace_id, "surface": {"$in": list(CLIENTS)}}
    if user_id:
        query["created_by"] = user_id
    rows = await api_keys._keys().find(query).sort("created_at", -1).to_list(length=60)
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "client": r["surface"], "label": r.get("client_label") or r.get("name"), "scopes": r.get("tool_scopes") or [], "created_by": r.get("created_by"),
            "created_at": api_keys._iso(r.get("created_at")), "last_used_at": api_keys._iso(r.get("last_used_at")), "expires_at": api_keys._iso((r.get("oauth") or {}).get("refresh_expires_at") or r.get("expires_at")),
            "revoked": bool(r.get("revoked")), "credits_this_month": 0 if r.get("revoked") else await ledger.spent_this_month(r["id"]),
        })
    return out


async def revoke(workspace_id: str, session_id: str, *, by: str, can_manage: bool) -> bool:
    """Revoke a sign-in. A person can revoke their own; revoking someone else's needs the right to manage keys. A repeat is a quiet yes."""
    row = await api_keys._keys().find_one({"id": session_id, "workspace_id": workspace_id, "surface": {"$in": list(CLIENTS)}})
    if row is None:
        return False
    if row.get("created_by") != by and not can_manage:
        raise SessionError("forbidden", "Only the person who signed in, or an owner or admin, can revoke this.", 403)
    await api_keys._keys().update_one({"id": session_id}, {"$set": {"revoked": True, "revoked_at": _now()}})
    return True


async def revoke_own(session_id: str) -> bool:
    """A session revokes itself. Only a command line or assistant sign-in can: a workspace key cannot be revoked this way."""
    done = await api_keys._keys().update_one({"id": session_id, "surface": {"$in": list(CLIENTS)}, "revoked": {"$ne": True}}, {"$set": {"revoked": True, "revoked_at": _now()}})
    return bool(done.modified_count)
