"""Telegram: the bot adapter and the parser for what Telegram posts to the webhook.

Telegram calls `/api/channels/telegram/webhook` for every message to the bot, and sends the secret chosen when the webhook was set in a header, so only
Telegram can call it. Replies go out through the Bot API. A voice note arrives as a file id; the adapter fetches it only when the engine asks."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx

from app.channels.base import Inbound
from app.core.config import settings

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
TIMEOUT = 15.0
MAX_BUTTONS = 3


def enabled() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_WEBHOOK_SECRET)


def verify(header_secret: Optional[str]) -> bool:
    """True when the request carries the secret this app gave Telegram. Compared in constant time."""
    if not settings.TELEGRAM_WEBHOOK_SECRET or not header_secret:
        return False
    return hmac.compare_digest(header_secret.encode("utf-8"), settings.TELEGRAM_WEBHOOK_SECRET.encode("utf-8"))


def parse(update: dict[str, Any]) -> Optional[Inbound]:
    """One Telegram update as a channel message, or None for anything Ekavo does not read (joins, edits, stickers)."""
    update_id = str(update.get("update_id") or "")
    if not update_id:
        return None
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        chat = ((callback.get("message") or {}).get("chat") or {}).get("id")
        if chat is None:
            return None
        return Inbound(channel="telegram", sender=str(chat), message_id=update_id, kind="button", payload=str(callback.get("data") or ""),
                       media={"callback_id": callback.get("id")})
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = (message.get("chat") or {}).get("id")
    if chat is None or (message.get("chat") or {}).get("type") != "private":
        return None                                              # only a one-to-one chat; a group or channel is never linked
    base = {"channel": "telegram", "sender": str(chat), "message_id": update_id}
    if isinstance(message.get("voice"), dict):
        return Inbound(**base, kind="voice", media={"id": message["voice"].get("file_id")})
    if isinstance(message.get("audio"), dict):
        return Inbound(**base, kind="voice", media={"id": message["audio"].get("file_id")})
    if message.get("photo"):
        return Inbound(**base, kind="image", media={"id": message["photo"][-1].get("file_id")}, text=str(message.get("caption") or ""))
    if isinstance(message.get("text"), str):
        return Inbound(**base, kind="text", text=message["text"])
    return None


class TelegramAdapter:
    name = "telegram"

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

    async def _call(self, method: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            async with self._http() as client:
                res = await client.post(f"{API}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}", json=payload)
            return res.json() if res.status_code == 200 else None
        except Exception as exc:  # noqa: BLE001 - a failed send is logged, never raised
            logger.warning("Telegram %s failed: %s", method, type(exc).__name__)
            return None

    async def send(self, to: str, text: str, buttons: Optional[list[tuple[str, str]]] = None) -> bool:
        payload: dict[str, Any] = {"chat_id": to, "text": text[:4000], "disable_web_page_preview": False}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label[:40], "callback_data": data[:64]}] for label, data in buttons[:MAX_BUTTONS]]}
        out = await self._call("sendMessage", payload)
        return bool(out and out.get("ok"))

    async def working(self, to: str, message_id: Optional[str] = None) -> None:
        """Shows "typing..." in the chat. Telegram keeps it for about 5 seconds. Best effort."""
        await self._call("sendChatAction", {"chat_id": to, "action": "typing"})

    async def answer_button(self, callback_id: Optional[str]) -> None:
        """Stops the button's spinner in Telegram. Best effort."""
        if callback_id:
            await self._call("answerCallbackQuery", {"callback_query_id": callback_id})

    async def fetch_media(self, media: dict[str, Any]) -> tuple[bytes, str]:
        file_id = str(media.get("id") or "")
        info = await self._call("getFile", {"file_id": file_id})
        path = ((info or {}).get("result") or {}).get("file_path")
        if not path:
            raise FileNotFoundError(file_id)
        async with self._http() as client:
            res = await client.get(f"{API}/file/bot{settings.TELEGRAM_BOT_TOKEN}/{path}")
        res.raise_for_status()
        return res.content, "voice.ogg"
