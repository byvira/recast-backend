"""Email in: forward a link or some text to the workspace's own address and it comes back as a draft in the Library.

Each workspace has a private address, `<local>+d-<code>@<inbound domain>`. The code is random, kept only as a hash, and can be changed in
Settings (the old address then stops working). A message becomes a draft only when ALL of these hold: Resend's signature is right (checked by
the route that receives it), the code matches a workspace, the sender is the verified email of an active member of that workspace who may make
content, the sender's own mail server did not fail its checks (SPF, DKIM or DMARC), the message was not seen before, and the sender is under
the hourly limit. Anything else is dropped without a reply, so nobody learns whether an address exists.

The draft is the text as sent: no model is called and no credits are spent. It goes through the same brand and safety checks as any draft
made by a tool. Approving it is never done by replying: the confirmation carries buttons (see links.py)."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.rbac import ROLE_PERMISSIONS
from app.db.mongo import get_db
from app.tools import executor, links
from app.tools.gatekeeper import Call, Gatekeeper, Refusal
from app.tools.registry import get_registry
from app.tools.services import LiveServices

logger = logging.getLogger(__name__)

CODE_IN_ADDRESS = re.compile(r"\+d-([A-Za-z0-9_-]{16,64})@")
PER_HOUR = 10
MAX_TEXT = 5000
_FAIL = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*fail\b", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def addresses():
    return get_db()["email_in_addresses"]


def seen():
    return get_db()["email_in_seen"]


async def create_indexes() -> None:
    await addresses().create_index("hash", unique=True)
    await addresses().create_index("workspace_id", unique=True)
    await seen().create_index("email_id", unique=True)
    await seen().create_index("at", expireAfterSeconds=30 * 24 * 3600)


def inbound_domain() -> str:
    """The domain Resend receives mail on for this app (the support address's domain)."""
    from app.core.notifications import sender_address

    return sender_address(settings.EMAIL_FROM).split("@", 1)[-1]


def address_for(code: str) -> str:
    return f"drafts+d-{code}@{inbound_domain()}"


async def rotate(workspace_id: str) -> str:
    """Make a new address for the workspace; the old one stops working at once. Returns the new address, shown to the owner."""
    code = secrets.token_urlsafe(18)
    await addresses().update_one({"workspace_id": workspace_id}, {"$set": {"workspace_id": workspace_id, "hash": _hash(code), "made_at": _now()}}, upsert=True)
    return address_for(code)


async def exists(workspace_id: str) -> bool:
    return await addresses().find_one({"workspace_id": workspace_id}, {"_id": 1}) is not None


def failed_checks(headers: Any) -> bool:
    """True when the message says its sender failed SPF, DKIM or DMARC. A message with no such header is not failed here; the secret code in
    the address is the second check."""
    if isinstance(headers, dict):
        text = " ".join(str(v) for k, v in headers.items() if str(k).lower() in ("authentication-results", "received-spf", "arc-authentication-results"))
    elif isinstance(headers, list):
        text = " ".join(str(h.get("value", "")) for h in headers if isinstance(h, dict) and str(h.get("name", "")).lower() in ("authentication-results", "received-spf"))
    else:
        text = ""
    return bool(_FAIL.search(text))


def draft_text(subject: str, body: str) -> str:
    """The words to save: the message body, or the subject when the body is empty (a forwarded link often has only that)."""
    text = (body or "").strip() or (subject or "").strip()
    return text[:MAX_TEXT]


async def _member(workspace_id: str, sender: str) -> Optional[dict[str, Any]]:
    db = get_db()
    user = await db["users"].find_one({"email": {"$regex": f"^{re.escape(sender)}$", "$options": "i"}}, {"id": 1, "email": 1})
    if not user:
        return None
    member = await db["workspace_members"].find_one({"workspace_id": workspace_id, "user_id": user["id"], "status": "active"}, {"role": 1})
    return {"user_id": user["id"], "role": member["role"]} if member else None


async def _target_brand(workspace_id: str) -> Optional[dict[str, Any]]:
    docs = await get_db()["brand_profiles"].find({"workspace_id": workspace_id}).sort([("is_default", -1), ("created_at", 1)]).limit(1).to_list(length=1)
    return docs[0] if docs else None


async def handle(data: dict[str, Any], fetch) -> str:
    """One received message. Returns what happened (never shown to the sender). `fetch(email_id)` reads the full message."""
    code = next((m.group(1) for m in (CODE_IN_ADDRESS.search(str(a)) for a in data.get("to") or []) if m), None)
    if not code:
        return "not_for_drafts"
    row = await addresses().find_one({"hash": _hash(code)}, {"workspace_id": 1})
    if not row:
        return "unknown_address"
    workspace_id = row["workspace_id"]
    email_id = str(data.get("email_id") or data.get("id") or "")
    sender = parseaddr(str(data.get("from") or ""))[1].strip().lower()
    if not email_id or not sender:
        return "ignored"
    try:
        await seen().insert_one({"email_id": email_id, "at": _now(), "workspace_id": workspace_id})
    except DuplicateKeyError:
        return "duplicate"
    member = await _member(workspace_id, sender)
    if member is None:
        return "sender_not_a_member"
    full = await fetch(email_id)
    if failed_checks(full.get("headers") or data.get("headers")):
        return "sender_failed_checks"
    if await seen().count_documents({"workspace_id": workspace_id, "sender": sender, "at": {"$gte": _now() - timedelta(hours=1)}}) >= PER_HOUR:
        return "limit"
    await seen().update_one({"email_id": email_id}, {"$set": {"sender": sender}})
    from app.core.notifications import send_templated_email
    from app.shared.support_inbound import _plain_from_html, reply_text

    body = reply_text(full.get("text") or _plain_from_html(full.get("html") or ""))
    text = draft_text(str(full.get("subject") or data.get("subject") or ""), body)
    if not text:
        return "empty"
    brand = await _target_brand(workspace_id)
    if brand is None:
        return "no_brand"
    platform = brand.get("default_platform") or (brand.get("platforms") or ["LinkedIn"])[0]
    gate = Gatekeeper(get_registry(), LiveServices())
    call = Call(credential_id=f"email:{workspace_id}:{member['user_id']}", tool="library.create_draft", arguments={"platform": platform, "brand_id": brand["id"], "content": text})
    try:
        _, outcome = await executor.run_call(gate, call)
    except (Refusal, executor.ToolFailure) as failure:
        logger.info("Email in could not make a draft: %s", getattr(failure, "code", failure))
        return "refused"
    piece_id = _piece_id(outcome.data if outcome else None)
    from app.core.config import settings

    app_url = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    # every variable is always sent, because a Resend template has no conditions: a sender who cannot approve gets "Open in Ekavo" and no second link
    variables: dict[str, Any] = {
        "platform": str(platform), "excerpt": text[:200],
        "approve_url": app_url, "approve_label": "Open in Ekavo", "send_back_url": app_url, "send_back_label": "",
        "note": "It is saved as a draft and waits for approval.",
    }
    if piece_id and "approve_content" in ROLE_PERMISSIONS.get(member["role"], set()):
        variables.update({
            "approve_url": await links.make(workspace_id, member["user_id"], "approve_post", piece_id), "approve_label": "Approve",
            "send_back_url": await links.make(workspace_id, member["user_id"], "send_back", piece_id), "send_back_label": "Send back",
            "note": "Approve it or send it back with one tap. The links work once and expire in 48 hours.",
        })
    await send_templated_email("email-in-saved", sender, variables)
    return "saved"


def _piece_id(data: Any) -> Optional[str]:
    """The id of the draft a call made, looked for in its answer or the item inside it."""
    if not isinstance(data, dict):
        return None
    for pool in [data, *[v for v in data.values() if isinstance(v, dict)]]:
        for key in ("piece_id", "id"):
            if isinstance(pool.get(key), str):
                return pool[key]
    return None
