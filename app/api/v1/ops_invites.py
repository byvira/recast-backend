"""Staff: invite people to Recast, see and manage the invites, and change the sign-up settings.

Invites are one-time codes sent by email. A batch stops at the daily cap so a mistake cannot use up the email quota. Everything here
writes to the lead audit trail.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import signup_invites, waitlist_leads
from app.shared import invites
from app.shared.lead_settings import SIGNUP_MODES, get_lead_settings, save_lead_settings
from app.shared.leads import record_event

router = APIRouter()
_NO_ID = {"_id": 0}
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class InviteBatch(BaseModel):
    lead_ids: list[str] = Field(..., min_length=1, max_length=50)


class DirectInvite(BaseModel):
    email: str = Field(..., max_length=254)

    @field_validator("email")
    @classmethod
    def _clean(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL.match(value):
            raise ValueError("Please enter a valid email address.")
        return value


class SettingsBody(BaseModel):
    signup_mode: Optional[str] = None
    invite_expiry_days: Optional[int] = Field(default=None, ge=1, le=60)
    daily_invite_cap: Optional[int] = Field(default=None, ge=1, le=500)
    digest_enabled: Optional[bool] = None
    digest_recipients: Optional[list[str]] = Field(default=None, max_length=10)

    @field_validator("signup_mode")
    @classmethod
    def _mode(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in SIGNUP_MODES:
            raise ValueError("Sign-up mode must be auto, open or invite.")
        return value

    @field_validator("digest_recipients")
    @classmethod
    def _recipients(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = sorted({item.strip().lower() for item in value if item.strip()})
        if any(not _EMAIL.match(item) for item in cleaned):
            raise ValueError("Every recipient must be a valid email address.")
        return cleaned


def _row(invite: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": invite["id"],
        "email": invite["email"],
        "lead_id": invite.get("lead_id"),
        "state": invites.invite_state(invite),
        "source": invite.get("source", "waitlist"),
        "created_at": invite.get("created_at"),
        "expires_at": invite.get("expires_at"),
        "sent_at": invite.get("sent_at"),
        "reminder_sent_at": invite.get("reminder_sent_at"),
        "used_at": invite.get("used_at"),
    }


async def _invited_today() -> int:
    return await signup_invites.count_documents({"created_at": {"$gte": datetime.now(timezone.utc) - timedelta(days=1)}})


async def _invite_one(lead: dict[str, Any], user: dict, source: str) -> tuple[bool, str]:
    """Create and send one invite. Returns whether it went and, if not, why."""
    invite, code = await invites.create_invite(lead, user, source)
    if not await invites.send_invite(lead, invite, code):
        await signup_invites.update_one({"id": invite["id"]}, {"$set": {"revoked": True}})
        return False, "The email could not be sent."
    await waitlist_leads.update_one({"id": lead["id"]}, {"$set": {"status": "invited", "invited_at": datetime.now(timezone.utc)}})
    await record_event("waitlist", lead["id"], "invited", user, f"Invite sent to {lead['email']}")
    return True, ""


@router.get("/invites")
@limiter.limit("60/minute")
async def list_invites(
    request: Request,
    state: Optional[str] = Query(None, max_length=10),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    docs = await signup_invites.find({}, _NO_ID).sort("created_at", -1).limit(500).to_list(500)
    rows = [_row(doc) for doc in docs]
    if state:
        rows = [row for row in rows if row["state"] == state]
    start = (page - 1) * page_size
    config = await get_lead_settings()
    return {"items": rows[start : start + page_size], "total": len(rows), "page": page, "page_size": page_size, "invited_today": await _invited_today(), "daily_cap": config["daily_invite_cap"]}


@router.post("/invites")
@limiter.limit("20/hour")
async def invite_leads(request: Request, body: InviteBatch, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Invite people from the waitlist. Each is checked on its own: one that cannot be invited does not stop the rest."""
    config = await get_lead_settings()
    room = max(0, int(config["daily_invite_cap"]) - await _invited_today())
    sent, skipped = 0, []
    for lead_id in dict.fromkeys(body.lead_ids):
        lead = await waitlist_leads.find_one({"id": lead_id}, _NO_ID)
        if not lead:
            skipped.append({"id": lead_id, "reason": "Not found."})
        elif lead.get("status", "joined") not in invites.INVITABLE:
            skipped.append({"id": lead_id, "reason": f"This person is {lead.get('status', 'joined')}, so they cannot be invited from here."})
        elif sent >= room:
            skipped.append({"id": lead_id, "reason": "The daily invite limit is reached."})
        else:
            ok, why = await _invite_one(lead, user, "waitlist")
            if ok:
                sent += 1
            else:
                skipped.append({"id": lead_id, "reason": why})
    return {"sent": sent, "skipped": skipped, "left_today": max(0, room - sent)}


@router.post("/invites/direct")
@limiter.limit("20/hour")
async def invite_address(request: Request, body: DirectInvite, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Invite someone who is not on the waitlist, such as a friend or a partner. They are added to the list so the history stays in one place."""
    config = await get_lead_settings()
    if await _invited_today() >= int(config["daily_invite_cap"]):
        raise HTTPException(status_code=429, detail="The daily invite limit is reached.")
    lead = await waitlist_leads.find_one({"email": body.email}, _NO_ID)
    if lead and lead.get("status", "joined") not in invites.INVITABLE:
        raise HTTPException(status_code=409, detail=f"This person is {lead.get('status', 'joined')}, so they cannot be invited from here.")
    if not lead:
        lead = {
            "id": str(uuid4()),
            "email": body.email,
            "referral_code": str(uuid4())[:8],
            "referred_by": None,
            "status": "joined",
            "notes": [],
            "referral_count": 0,
            "source": "staff",
            "utm": {},
            "role": None,
            "team_size": None,
            "platforms": [],
            "created_at": datetime.now(timezone.utc),
        }
        await waitlist_leads.insert_one(dict(lead))
    ok, why = await _invite_one(lead, user, "staff")
    if not ok:
        raise HTTPException(status_code=502, detail=why)
    return {"ok": True, "lead_id": lead["id"]}


@router.post("/invites/{invite_id}/resend")
@limiter.limit("20/hour")
async def resend_invite(request: Request, invite_id: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Send a fresh invite to the same person. The earlier code stops working."""
    invite = await signup_invites.find_one({"id": invite_id}, _NO_ID)
    if not invite:
        raise HTTPException(status_code=404, detail="That invite was not found.")
    if invites.invite_state(invite) == "used":
        raise HTTPException(status_code=409, detail="This invite was already used.")
    lead = await waitlist_leads.find_one({"id": invite["lead_id"]}, _NO_ID)
    if not lead or lead.get("status") == "unsubscribed":
        raise HTTPException(status_code=409, detail="This person cannot be emailed.")
    ok, why = await _invite_one(lead, user, invite.get("source", "waitlist"))
    if not ok:
        raise HTTPException(status_code=502, detail=why)
    return {"ok": True}


@router.post("/invites/{invite_id}/revoke")
@limiter.limit("30/hour")
async def revoke_invite(request: Request, invite_id: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    invite = await signup_invites.find_one({"id": invite_id}, _NO_ID)
    if not invite:
        raise HTTPException(status_code=404, detail="That invite was not found.")
    if invites.invite_state(invite) != "sent":
        raise HTTPException(status_code=409, detail="Only an invite that is still open can be revoked.")
    await signup_invites.update_one({"id": invite_id}, {"$set": {"revoked": True}})
    await waitlist_leads.update_one({"id": invite["lead_id"], "status": "invited"}, {"$set": {"status": "reviewed"}})
    await record_event("waitlist", invite["lead_id"], "invite revoked", user)
    return {"ok": True}


@router.get("/settings")
@limiter.limit("60/minute")
async def get_settings_route(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    config = await get_lead_settings()
    return {**config, "effective_signup_mode": await invites.signup_mode()}


@router.put("/settings")
@limiter.limit("30/hour")
async def put_settings(request: Request, body: SettingsBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    if changes:
        await save_lead_settings(changes)
        await record_event("settings", "*", "settings", user, ", ".join(sorted(changes)))
    return await get_settings_route(request, user)
