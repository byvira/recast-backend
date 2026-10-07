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

logger = logging.getLogger(__name__)
router = APIRouter()

NOTIFY_TEMPLATE = "contact-message"


@router.post("")
@router.post("/", include_in_schema=False)
@limiter.limit("5/minute;20/hour")
async def send_contact_message(request: Request, body: ContactBody) -> dict[str, bool]:
    ip = request.client.host if request.client else ""
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(status_code=400, detail="Please complete the security check and try again.")

    await contact_messages.insert_one(
        {
            "id": str(uuid4()),
            "name": body.name,
            "email": body.email,
            "message": body.message,
            "topic": body.topic,
            "created_at": datetime.now(timezone.utc),
        }
    )
    inbox = (settings.CONTACT_INBOX_EMAIL or "").strip()
    if inbox:
        try:
            await send_templated_email(NOTIFY_TEMPLATE, inbox, {"NAME": body.name, "EMAIL": body.email, "TOPIC": body.topic, "MESSAGE": body.message})
        except Exception:  # noqa: BLE001 - the message is already saved; a failed notice must not lose it
            logger.warning("Contact notice could not be sent", exc_info=True)
    return {"ok": True}
