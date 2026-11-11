"""The address Resend calls when someone replies to a support email. Public, but every call must carry Resend's signature."""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.core.middleware import limiter
from app.shared import support_inbound

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/inbound-email")
@limiter.limit("300/minute")
async def inbound_email(request: Request) -> dict:
    if not settings.SUPPORT_INBOUND_WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="Not found.")
    body = await request.body()
    if not support_inbound.verify_signature(body, dict(request.headers), settings.SUPPORT_INBOUND_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Signature does not match.")
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Not a valid message.")
    try:
        outcome = await support_inbound.handle_event(event if isinstance(event, dict) else {})
    except Exception:  # noqa: BLE001
        logger.warning("A support email reply could not be applied; Resend will send it again", exc_info=True)
        raise HTTPException(status_code=500, detail="Try again.")
    return {"ok": True, "result": outcome}
