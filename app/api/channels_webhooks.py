"""Where the chat services call in: `/api/channels/whatsapp/webhook`, `/api/channels/telegram/webhook` and (only when switched on, never in production) a test sender.

Each service proves a call is really from it (Meta signs the body, Telegram sends a secret). A call that does not prove it is refused before anything is read,
and a channel with no keys set answers 404. The reply to the service is always quick: the message is handled in the background, because the services
resend a message that is not acknowledged in a few seconds (and the engine ignores a repeat anyway)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.channels import engine, telegram, whatsapp
from app.channels.base import Inbound
from app.channels.simulator import SimulatorAdapter
from app.core.config import settings
from app.core.middleware import limiter

logger = logging.getLogger(__name__)
router = APIRouter()
_tasks: set[asyncio.Task] = set()


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _safely(adapter: Any, msg: Inbound) -> None:
    try:
        await engine.handle(adapter, msg)
    except Exception:  # noqa: BLE001 - one bad message never stops the rest
        logger.exception("A channel message could not be handled (%s)", msg.channel)


@router.get("/whatsapp/webhook")
@limiter.limit("60/minute")
async def whatsapp_verify(request: Request, hub_mode: Optional[str] = Query(None, alias="hub.mode"), hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
                          hub_challenge: Optional[str] = Query(None, alias="hub.challenge")) -> PlainTextResponse:
    """Meta's check that this address is yours, done once when the webhook is set up."""
    answer = whatsapp.verify_challenge(hub_mode, hub_verify_token, hub_challenge)
    if answer is None:
        raise HTTPException(status_code=403, detail="Not verified.")
    return PlainTextResponse(answer)


@router.post("/whatsapp/webhook")
@limiter.limit("600/minute")
async def whatsapp_in(request: Request, x_hub_signature_256: Optional[str] = Header(None)) -> dict[str, bool]:
    if not whatsapp.enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    body = await request.body()
    if not whatsapp.verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Bad signature.")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad body.")
    for code, title in whatsapp.failed_deliveries(payload if isinstance(payload, dict) else {}):
        logger.warning("WhatsApp could not deliver a reply: %s %s", code, title)
    adapter = whatsapp.WhatsAppAdapter()
    for msg in whatsapp.parse(payload if isinstance(payload, dict) else {}):
        _background(_safely(adapter, msg))
    return {"ok": True}


@router.post("/telegram/webhook")
@limiter.limit("600/minute")
async def telegram_in(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)) -> dict[str, bool]:
    if not telegram.enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    if not telegram.verify(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="Bad secret.")
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad body.")
    msg = telegram.parse(update if isinstance(update, dict) else {})
    if msg is not None:
        adapter = telegram.TelegramAdapter()
        if msg.kind == "button":
            await adapter.answer_button((msg.media or {}).get("callback_id"))
        _background(_safely(adapter, msg))
    return {"ok": True}


@router.post("/simulator/inbound")
@limiter.limit("120/minute")
async def simulator_in(request: Request) -> dict[str, Any]:
    """Posts one message as a pretend chat and returns what Ekavo would have said. Only when CHANNELS_SIMULATOR is on, and never in production."""
    if not settings.CHANNELS_SIMULATOR or settings.ENVIRONMENT == "production":
        raise HTTPException(status_code=404, detail="Not found.")
    data = await request.json()
    adapter = SimulatorAdapter()
    msg = Inbound(channel="simulator", sender=str(data.get("sender") or "sim-1"), message_id=str(data.get("message_id") or ""), kind=str(data.get("kind") or "text"),
                  text=str(data.get("text") or ""), payload=str(data.get("payload") or ""))
    if not msg.message_id:
        raise HTTPException(status_code=422, detail="message_id is needed.")
    outcome = await engine.handle(adapter, msg, wait=True)
    return {"outcome": outcome, "sent": adapter.sent}
