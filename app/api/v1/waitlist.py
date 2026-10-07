"""The public waitlist: join with an email, then optionally say who you are. No sign-in needed."""

import logging
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.core.turnstile import verify_turnstile
from app.db.mongo import waitlist_leads
from app.models.waitlist import WaitlistJoinBody, WaitlistJoinResponse, WaitlistProfileBody
from app.shared import invites
from app.shared.leads import record_event

logger = logging.getLogger(__name__)
router = APIRouter()

WELCOME_TEMPLATE = "waitlist-welcome"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@router.post("", response_model=WaitlistJoinResponse)
@router.post("/", response_model=WaitlistJoinResponse, include_in_schema=False)
@limiter.limit("10/minute;60/hour")
async def join_waitlist(request: Request, body: WaitlistJoinBody) -> WaitlistJoinResponse:
    """Add an email to the waitlist.

    The bot check is required once a Turnstile secret is set. Joining twice with the same address is not an error, but the second time the
    share code is not handed out again: anyone who knows an address could otherwise read it back.
    """
    if not await verify_turnstile(body.turnstile_token, _client_ip(request)):
        raise HTTPException(status_code=400, detail="Please complete the security check and try again.")

    if await waitlist_leads.find_one({"email": body.email}, {"_id": 1}):
        return WaitlistJoinResponse(status="already_joined")

    referrer = None
    if body.referral_code:
        referrer = await waitlist_leads.find_one({"referral_code": body.referral_code}, {"referral_code": 1, "email": 1})
        if referrer and referrer["email"] == body.email:
            referrer = None

    code = secrets.token_urlsafe(6)
    lead = {
        "id": str(uuid4()),
        "email": body.email,
        "referral_code": code,
        "referred_by": referrer["referral_code"] if referrer else None,
        "status": "joined",
        "notes": [],
        "referral_count": 0,
        "source": body.source,
        "utm": body.utm.model_dump(exclude_none=True) if body.utm else {},
        "role": None,
        "team_size": None,
        "platforms": [],
        "created_at": datetime.now(timezone.utc),
    }
    try:
        await waitlist_leads.insert_one(lead)
    except DuplicateKeyError:
        # Two requests for the same address arrived together; the first one won.
        return WaitlistJoinResponse(status="already_joined")

    if referrer:
        await waitlist_leads.update_one({"referral_code": referrer["referral_code"]}, {"$inc": {"referral_count": 1}})

    try:
        await send_templated_email(WELCOME_TEMPLATE, body.email, {"REFERRAL_CODE": code, "UNSUBSCRIBE_URL": invites.unsubscribe_url(lead["id"])})
    except Exception:  # noqa: BLE001 - a failed welcome email must never lose the lead
        logger.warning("Waitlist welcome email could not be sent", exc_info=True)

    return WaitlistJoinResponse(status="joined", referral_code=code)


@router.put("/{code}/profile")
@limiter.limit("20/minute")
async def save_waitlist_profile(request: Request, code: str, body: WaitlistProfileBody) -> dict[str, bool]:
    """Save the optional answers (role, team size, platforms). The share code returned when joining proves the entry is yours."""
    changes = {key: value for key, value in body.model_dump().items() if value not in (None, [])}
    if not changes:
        return {"ok": True}
    result = await waitlist_leads.update_one({"referral_code": code}, {"$set": changes})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="That link is not valid.")
    # A team of six or more, or an agency, is worth knowing about straight away.
    lead = await waitlist_leads.find_one({"referral_code": code}, {"_id": 0})
    if lead:
        await invites.alert_big_team(lead)
    return {"ok": True}


class UnsubscribeBody(BaseModel):
    token: str = Field(..., max_length=200)


@router.get("/unsubscribe")
@limiter.limit("30/minute")
async def unsubscribe_info(request: Request, t: str = Query("", max_length=200)) -> dict[str, str]:
    """Who this unsubscribe link is for, with the address partly hidden. Opening the link changes nothing: only the button does."""
    lead_id = invites.lead_id_from_token(t)
    lead = await waitlist_leads.find_one({"id": lead_id}, {"email": 1, "status": 1}) if lead_id else None
    if not lead:
        raise HTTPException(status_code=404, detail="That link is not valid.")
    name, _, domain = lead["email"].partition("@")
    return {"email": f"{name[:2]}***@{domain}", "status": lead.get("status", "joined")}


@router.post("/unsubscribe")
@limiter.limit("10/minute")
async def unsubscribe(request: Request, body: UnsubscribeBody) -> dict[str, bool]:
    """Stop all emails to this person. It is a button on a page, not the link itself, so a mail scanner opening the link cannot do it."""
    lead_id = invites.lead_id_from_token(body.token)
    if not lead_id:
        raise HTTPException(status_code=404, detail="That link is not valid.")
    result = await waitlist_leads.update_one({"id": lead_id}, {"$set": {"status": "unsubscribed", "unsubscribed_at": datetime.now(timezone.utc)}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="That link is not valid.")
    await record_event("waitlist", lead_id, "unsubscribed", {"id": "", "name": "The person"}, "Used the link in an email")
    return {"ok": True}
