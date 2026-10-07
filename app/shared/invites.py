"""Invite codes, unsubscribe links and the lead emails that go with them.

An invite is a one-time code tied to an email address. Only its hash is stored, so a copy of the database cannot be used to sign up.
Sending, expiring and reminding all live here so the Ops screens, the sign-up check and the background jobs agree on the rules.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from app.core.config import settings
from app.core.notifications import send_templated_email
from app.db.mongo import contact_messages, signup_invites, waitlist_leads
from app.shared.lead_settings import effective_signup_mode, get_lead_settings
from app.shared.leads import record_event

logger = logging.getLogger(__name__)

INVITE_TEMPLATE = "waitlist-invite"
REMINDER_TEMPLATE = "waitlist-reminder"
DIGEST_TEMPLATE = "lead-digest"
ALERT_TEMPLATE = "lead-alert"
REMINDER_AFTER = timedelta(days=7)
# Statuses a person can be invited from. Invited, activated and unsubscribed people are left alone.
INVITABLE = ("joined", "reviewed", "hold", "expired")
BIG_TEAM_SIZES = ("6-20", "21+")


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def frontend(path: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


def invite_url(code: str) -> str:
    return frontend(f"/signup?invite={code}")


def invite_state(invite: dict[str, Any], now: Optional[datetime] = None) -> str:
    """sent, used, expired or revoked: what the Ops screen shows for an invite."""
    now = now or datetime.now(timezone.utc)
    if invite.get("used_at"):
        return "used"
    if invite.get("revoked"):
        return "revoked"
    expires = _aware(invite.get("expires_at"))
    return "expired" if expires is not None and expires <= now else "sent"


# Unsubscribe links

def _sign(lead_id: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), f"unsubscribe:{lead_id}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def unsubscribe_token(lead_id: str) -> str:
    return f"{lead_id}.{_sign(lead_id)}"


def lead_id_from_token(token: str) -> Optional[str]:
    lead_id, _, signature = (token or "").partition(".")
    if lead_id and signature and hmac.compare_digest(signature, _sign(lead_id)):
        return lead_id
    return None


def unsubscribe_url(lead_id: str) -> str:
    return frontend(f"/unsubscribe?t={unsubscribe_token(lead_id)}")


# Creating and sending invites

def _display_name(email: str) -> str:
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return local.title() or "there"


def _expires_text(when: datetime) -> str:
    return f"{when.day} {when.strftime('%B %Y')}"


async def create_invite(lead: dict[str, Any], actor: Optional[dict], source: str) -> tuple[dict[str, Any], str]:
    """Make a fresh code for this lead. Any earlier open invite for the same address is revoked, so only the newest one works."""
    config = await get_lead_settings()
    now = datetime.now(timezone.utc)
    code = secrets.token_urlsafe(18)
    invite = {
        "id": str(uuid4()),
        "code_hash": hash_code(code),
        "email": lead["email"],
        "lead_id": lead["id"],
        "source": source,
        "created_by": (actor or {}).get("id"),
        "created_at": now,
        "expires_at": now + timedelta(days=int(config["invite_expiry_days"])),
        "used_at": None,
        "used_by": None,
        "revoked": False,
        "sent_at": None,
        "reminder_sent_at": None,
    }
    await signup_invites.update_many({"email": lead["email"], "used_at": None, "revoked": False}, {"$set": {"revoked": True}})
    await signup_invites.insert_one(invite)
    return invite, code


async def send_invite(lead: dict[str, Any], invite: dict[str, Any], code: str, *, reminder: bool = False) -> bool:
    """Send the invite (or the reminder) email. The invite is marked sent only when the email went."""
    variables = {
        "NAME": _display_name(lead["email"]),
        "INVITE_URL": invite_url(code),
        "EXPIRES": _expires_text(_aware(invite["expires_at"])),
        "UNSUBSCRIBE_URL": unsubscribe_url(lead["id"]),
    }
    try:
        sent = await send_templated_email(REMINDER_TEMPLATE if reminder else INVITE_TEMPLATE, lead["email"], variables)
    except Exception:  # noqa: BLE001
        logger.warning("Invite email could not be sent", exc_info=True)
        sent = False
    if sent:
        field = "reminder_sent_at" if reminder else "sent_at"
        await signup_invites.update_one({"id": invite["id"]}, {"$set": {field: datetime.now(timezone.utc)}})
    return bool(sent)


# Checking and using a code at sign-up

async def find_open_invite(code: str) -> Optional[dict[str, Any]]:
    if not code or len(code) > 200:
        return None
    invite = await signup_invites.find_one({"code_hash": hash_code(code.strip())}, {"_id": 0})
    return invite if invite and invite_state(invite) == "sent" else None


async def signup_mode() -> str:
    return effective_signup_mode((await get_lead_settings())["signup_mode"])


async def check_signup_allowed(identifier: str, channel_is_email: bool, code: Optional[str]) -> Optional[dict[str, Any]]:
    """Run before an account is created. Returns the invite to use up afterwards, or None when sign-up is open and no code was given."""
    invite = await find_open_invite(code or "")
    if invite and channel_is_email and invite["email"] != identifier:
        invite = None
    if await signup_mode() == "invite" and invite is None:
        raise HTTPException(status_code=403, detail="Sign-up is by invite right now. Join the waitlist, or use the invite we sent you.")
    return invite


async def use_invite(invite: dict[str, Any], user_id: str) -> None:
    """Spend the invite and mark the person as activated. The update only matches an unused invite, so a code works once."""
    now = datetime.now(timezone.utc)
    result = await signup_invites.update_one({"id": invite["id"], "used_at": None}, {"$set": {"used_at": now, "used_by": user_id}})
    if result.modified_count == 1 and invite.get("lead_id"):
        await waitlist_leads.update_one({"id": invite["lead_id"]}, {"$set": {"status": "activated", "activated_user_id": user_id, "activated_at": now}})
        await record_event("waitlist", invite["lead_id"], "activated", {"id": user_id, "name": "Sign-up"}, "Signed up with the invite")


# Background work

async def sweep_invites() -> dict[str, int]:
    """Expire invites that ran out, and send one reminder to invites that are a week old and still unused."""
    now = datetime.now(timezone.utc)
    expired = 0
    stale = {"used_at": None, "revoked": False, "expires_at": {"$lte": now}, "expired_at": {"$exists": False}}
    async for invite in signup_invites.find(stale, {"_id": 0}).limit(500):
        await signup_invites.update_one({"id": invite["id"]}, {"$set": {"expired_at": now}})
        result = await waitlist_leads.update_one({"id": invite["lead_id"], "status": "invited"}, {"$set": {"status": "expired"}})
        if result.modified_count:
            await record_event("waitlist", invite["lead_id"], "expired", {"id": "", "name": "Schedule"}, "Invite not used in time")
        expired += 1

    reminded = 0
    due = {"used_at": None, "revoked": False, "expires_at": {"$gt": now}, "sent_at": {"$lte": now - REMINDER_AFTER}, "reminder_sent_at": None}
    async for invite in signup_invites.find(due, {"_id": 0}).limit(200):
        lead = await waitlist_leads.find_one({"id": invite["lead_id"]}, {"_id": 0})
        if not lead or lead.get("status") != "invited":
            continue
        # The code itself is not kept, so a reminder needs a fresh one. The old code stops working and the new one has the same end date.
        fresh, code = await create_invite(lead, None, "reminder")
        await signup_invites.update_one({"id": fresh["id"]}, {"$set": {"expires_at": invite["expires_at"], "sent_at": invite["sent_at"]}})
        fresh["expires_at"] = invite["expires_at"]
        if await send_invite(lead, fresh, code, reminder=True):
            reminded += 1
    return {"expired": expired, "reminded": reminded}


async def lead_digest() -> bool:
    """One email a day to the team: what came in, and what is waiting. Skipped when there is nothing to say."""
    config = await get_lead_settings()
    recipients = [address for address in config.get("digest_recipients", []) if address]
    if not config.get("digest_enabled") or not recipients:
        return False
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)
    new_waitlist = await waitlist_leads.count_documents({"created_at": {"$gte": since}})
    new_messages = await contact_messages.count_documents({"created_at": {"$gte": since}})
    sales_open = await contact_messages.count_documents({"topic": "seats", "status": {"$in": ["new", "open"]}})
    overdue = await contact_messages.count_documents({"status": {"$in": ["new", "open"]}, "created_at": {"$lt": now - timedelta(days=3)}})
    if not (new_waitlist or new_messages or sales_open or overdue):
        return False
    pipeline = [{"$match": {"created_at": {"$gte": since}}}, {"$group": {"_id": {"$ifNull": ["$source", "unknown"]}, "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 4}]
    top = ", ".join([f"{row['_id']} {row['n']}" async for row in waitlist_leads.aggregate(pipeline)]) or "none"
    variables = {
        "NEW_WAITLIST": str(new_waitlist),
        "NEW_MESSAGES": str(new_messages),
        "SALES_OPEN": str(sales_open),
        "OVERDUE": str(overdue),
        "TOP_SOURCES": top,
        "OPS_URL": frontend("/ops/leads"),
    }
    sent = False
    for address in recipients:
        try:
            sent = bool(await send_templated_email(DIGEST_TEMPLATE, address, variables)) or sent
        except Exception:  # noqa: BLE001
            logger.warning("Lead digest could not be sent to one recipient", exc_info=True)
    return sent


async def alert_big_team(lead: dict[str, Any]) -> None:
    """Tell the team straight away when someone with a team of six or more (or an agency) joins. Once per lead."""
    config = await get_lead_settings()
    recipients = [address for address in config.get("digest_recipients", []) if address]
    if lead.get("alerted") or not recipients:
        return
    if lead.get("team_size") not in BIG_TEAM_SIZES and lead.get("role") != "agency":
        return
    await waitlist_leads.update_one({"id": lead["id"]}, {"$set": {"alerted": True}})
    variables = {
        "EMAIL": lead["email"],
        "ROLE": lead.get("role") or "not said",
        "TEAM_SIZE": lead.get("team_size") or "not said",
        "OPS_URL": frontend(f"/ops/leads/waitlist/{lead['id']}"),
    }
    for address in recipients:
        try:
            await send_templated_email(ALERT_TEMPLATE, address, variables)
        except Exception:  # noqa: BLE001
            logger.warning("Big team alert could not be sent", exc_info=True)
