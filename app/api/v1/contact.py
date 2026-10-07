"""The public contact form: stores the message so it is never lost, and passes it to the team inbox when one is set."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.core.turnstile import verify_turnstile
from app.db.mongo import contact_messages
from app.models.contact import ContactBody
from app.models.leads import CONTACT_TOPICS
from app.shared.leads import next_contact_reference

logger = logging.getLogger(__name__)
router = APIRouter()

NOTIFY_TEMPLATE = "contact-message"
ACK_TEMPLATE = "contact-received"


@router.post("")
@router.post("/", include_in_schema=False)
@limiter.limit("5/minute;20/hour")
async def send_contact_message(request: Request, body: ContactBody) -> dict:
    ip = request.client.host if request.client else ""
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(status_code=400, detail="Please complete the security check and try again.")

    reference = await next_contact_reference()
    topic = body.topic if body.topic in CONTACT_TOPICS else "general"
    await contact_messages.insert_one(
        {
            "id": str(uuid4()),
            "reference": reference,
            "status": "new",
            "assignee": None,
            "internal_notes": [],
            "replies": [],
            "answered_at": None,
            "name": body.name,
            "email": body.email,
            "message": body.message,
            "topic": topic,
            "created_at": datetime.now(timezone.utc),
        }
    )
    inbox = (settings.CONTACT_INBOX_EMAIL or "").strip()
    if inbox:
        try:
            await send_templated_email(NOTIFY_TEMPLATE, inbox, {"NAME": body.name, "EMAIL": body.email, "TOPIC": topic, "MESSAGE": body.message, "REFERENCE": reference})
        except Exception:  # noqa: BLE001 - the message is already saved; a failed notice must not lose it
            logger.warning("Contact notice could not be sent", exc_info=True)
    # The person is told it arrived. A failure here changes nothing: the message is saved and the screen already thanks them.
    try:
        await send_templated_email(ACK_TEMPLATE, body.email, {"NAME": body.name, "REFERENCE": reference})
    except Exception:  # noqa: BLE001
        logger.warning("Contact acknowledgement could not be sent", exc_info=True)
    return {"ok": True, "reference": reference}
