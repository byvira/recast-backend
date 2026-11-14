"""WhatsApp (Meta's official Cloud API): the adapter, the signature check and the parser for what Meta posts to the webhook.

Meta calls `/api/channels/whatsapp/webhook`. A GET with the verify token you chose proves the address is yours; every POST carries an
`X-Hub-Signature-256` header, an HMAC of the exact body made with the app secret, so only Meta can post a message. Replies go out through the Graph API.

Meta's free test number works for up to five recipients before business verification (see docs/runbooks/CHANNELS_RUNBOOK.md). Inside 24 hours of the
person's last message a free-form reply is allowed; this adapter only replies to messages, so it always is. Messages Ekavo starts on its own (reminders)
need approved templates and are a later stage."""
from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx

from app.channels.base import Inbound
from app.core.config import settings

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
GRAPH_VERSION = "v21.0"                         # confirm against Meta's current version when the app is made
TIMEOUT = 15.0
MAX_BUTTONS = 3
BODY_LIMIT = 1000


def enabled() -> bool:
    return bool(settings.WHATSAPP_APP_SECRET and settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID)


def verify_challenge(mode: Optional[str], token: Optional[str], challenge: Optional[str]) -> Optional[str]:
    """The answer to Meta's address check: the challenge when the mode and verify token are right, else None."""
    if mode == "subscribe" and settings.WHATSAPP_VERIFY_TOKEN and token and hmac.compare_digest(token, settings.WHATSAPP_VERIFY_TOKEN):
        return challenge or ""
    return None


def verify_signature(body: bytes, header: Optional[str]) -> bool:
    """True when `header` (`sha256=<hex>`) is the HMAC of the exact body with the app secret."""
    if not settings.WHATSAPP_APP_SECRET or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.WHATSAPP_APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def parse(payload: dict[str, Any]) -> list[Inbound]:
    """Every message in one webhook call, as channel messages. Delivery receipts and message kinds Ekavo does not read are skipped."""
    out: list[Inbound] = []
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            for item in ((change or {}).get("value") or {}).get("messages") or []:
                if not isinstance(item, dict) or not item.get("from") or not item.get("id"):
                    continue
                base = {"channel": "whatsapp", "sender": str(item["from"]), "message_id": str(item["id"])}
                kind = item.get("type")
                if kind == "text":
                    out.append(Inbound(**base, kind="text", text=str((item.get("text") or {}).get("body") or "")))
                elif kind == "audio":
                    out.append(Inbound(**base, kind="voice", media={"id": (item.get("audio") or {}).get("id")}))
                elif kind == "image":
                    out.append(Inbound(**base, kind="image", media={"id": (item.get("image") or {}).get("id")}, text=str((item.get("image") or {}).get("caption") or "")))
                elif kind == "interactive":
                    reply = (item.get("interactive") or {}).get("button_reply") or {}
                    if reply.get("id"):
                        out.append(Inbound(**base, kind="button", payload=str(reply["id"])))
                elif kind == "button":
                    if (item.get("button") or {}).get("payload"):
                        out.append(Inbound(**base, kind="button", payload=str(item["button"]["payload"])))
    return out


class WhatsAppAdapter:
    name = "whatsapp"

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client

    @asynccontextmanager
    async def _http(self):
        """The client given at construction (tests) is used as it is; otherwise the shared pool is, so a call reuses an open connection. Never closed here."""
        if self._client is not None:
            yield self._client
        else:
            from app.channels import pool

            yield pool.shared(TIMEOUT)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}

    async def send(self, to: str, text: str, buttons: Optional[list[tuple[str, str]]] = None) -> bool:
        body: dict[str, Any] = {"messaging_product": "whatsapp", "to": to}
        if buttons:
            body.update(type="interactive", interactive={
                "type": "button", "body": {"text": text[:BODY_LIMIT]},
                "action": {"buttons": [{"type": "reply", "reply": {"id": data[:256], "title": label[:20]}} for label, data in buttons[:MAX_BUTTONS]]},
            })
        else:
            body.update(type="text", text={"body": text[:4000], "preview_url": True})
        try:
            async with self._http() as client:
                res = await client.post(f"{GRAPH}/{GRAPH_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages", headers=self._headers(), json=body)
            if res.status_code >= 300:
                logger.warning("WhatsApp send was refused: %s", res.status_code)
            return res.status_code < 300
        except Exception as exc:  # noqa: BLE001 - a failed send is logged, never raised
            logger.warning("WhatsApp send failed: %s", type(exc).__name__)
            return False

    #: WhatsApp shows "typing..." for up to 25 seconds, so it is asked again well before that
    typing_seconds = 20.0

    async def working(self, to: str, message_id: Optional[str] = None) -> None:
        """Marks the person's message as read (the blue ticks) and shows "typing...". Best effort: a refusal is only logged."""
        if not message_id:
            return
        body = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id, "typing_indicator": {"type": "text"}}
        try:
            async with self._http() as client:
                res = await client.post(f"{GRAPH}/{GRAPH_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages", headers=self._headers(), json=body)
            if res.status_code >= 300:
                logger.info("WhatsApp typing signal was refused: %s", res.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.info("WhatsApp typing signal failed: %s", type(exc).__name__)

    async def send_template(self, to: str, name: str, params: list[str], language: str = "en") -> bool:
        """An approved template message, the only kind WhatsApp allows after 24 hours. Each one is charged by Meta, so callers cap them. Never raises."""
        body = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
            "name": name, "language": {"code": language}, "components": [{"type": "body", "parameters": [{"type": "text", "text": p[:200]} for p in params]}]}}
        try:
            async with self._http() as client:
                res = await client.post(f"{GRAPH}/{GRAPH_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages", headers=self._headers(), json=body)
            return res.status_code < 300
        except Exception as exc:  # noqa: BLE001
            logger.warning("WhatsApp template send failed: %s", type(exc).__name__)
            return False

    async def fetch_media(self, media: dict[str, Any]) -> tuple[bytes, str]:
        media_id = str(media.get("id") or "")
        async with self._http() as client:
            meta = await client.get(f"{GRAPH}/{GRAPH_VERSION}/{media_id}", headers=self._headers())
            meta.raise_for_status()
            url = str(meta.json().get("url") or "")
            if not url.startswith("https://"):
                raise FileNotFoundError(media_id)
            data = await client.get(url, headers=self._headers())
        data.raise_for_status()
        return data.content, "voice.ogg"
