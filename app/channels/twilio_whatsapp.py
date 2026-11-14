"""WhatsApp through Twilio: the adapter, the signature check and the parser for what Twilio posts to the webhook.

Used for testing with Twilio's WhatsApp sandbox (a shared number people join with a code) and, later, a sender registered through Twilio. It answers on the same
"whatsapp" channel as Meta's own API, so a chat linked here is linked the same way. Twilio calls `/api/channels/twilio-whatsapp/webhook` with form fields and an
`X-Twilio-Signature` header made with the account's auth token. Replies go out through Twilio's Messages API.

A session message has no typing signal and no buttons here, so a reply that offers buttons lists them as numbered choices, and the number the person sends back
is turned into the button press (kept in memory for a short while, per chat)."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Mapping, Optional

import httpx

from app.channels.base import Inbound
from app.core.config import settings

logger = logging.getLogger(__name__)

API = "https://api.twilio.com/2010-04-01"
TIMEOUT = 15.0
MAX_BUTTONS = 3
BODY_LIMIT = 1500                       # Twilio refuses a WhatsApp body over 1600 characters
CHOICE_MINUTES = 30
_choices: dict[str, tuple[float, dict[str, str]]] = {}


def enabled() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_WHATSAPP_NUMBER)


def verify_signature(url: str, params: Mapping[str, str], header: Optional[str]) -> bool:
    """True when `header` is Twilio's signature of this address and these form fields, made with the account's auth token."""
    if not settings.TWILIO_AUTH_TOKEN or not header:
        return False
    from twilio.request_validator import RequestValidator

    return bool(RequestValidator(settings.TWILIO_AUTH_TOKEN).validate(url, dict(params), header))


def _number(value: str) -> str:
    """`whatsapp:+919999900001` as the digits the engine uses for a chat."""
    return "".join(c for c in (value or "") if c.isdigit())


def remember_choices(sender: str, buttons: list[tuple[str, str]]) -> None:
    now = time.monotonic()
    for key in [k for k, (until, _) in _choices.items() if until < now]:
        _choices.pop(key, None)
    _choices[sender] = (now + CHOICE_MINUTES * 60, {str(i + 1): data for i, (_label, data) in enumerate(buttons[:MAX_BUTTONS])})


def parse(form: Mapping[str, str]) -> Optional[Inbound]:
    """The channel message in one Twilio webhook call, or None for a call that is not a message (a delivery update)."""
    sender = _number(form.get("From", ""))
    message_id = form.get("MessageSid") or form.get("SmsMessageSid") or ""
    if not sender or not message_id:
        return None
    base = {"channel": "whatsapp", "sender": sender, "message_id": message_id}
    body = (form.get("Body") or "").strip()
    try:
        media_count = int(form.get("NumMedia") or 0)
    except ValueError:
        media_count = 0
    if media_count > 0 and form.get("MediaUrl0"):
        kind = str(form.get("MediaContentType0") or "")
        if kind.startswith("audio/"):
            return Inbound(**base, kind="voice", media={"url": form["MediaUrl0"]})
        if kind.startswith("image/"):
            return Inbound(**base, kind="image", media={"url": form["MediaUrl0"]}, text=body)
        return None
    if body:
        held = _choices.get(sender)
        if held and held[0] > time.monotonic() and body in held[1]:
            return Inbound(**base, kind="button", payload=held[1][body])
        return Inbound(**base, kind="text", text=body)
    return None


class TwilioWhatsAppAdapter:
    name = "whatsapp"
    #: no typing signal exists here, so the engine has nothing to repeat
    typing_seconds = 3600.0

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client

    @asynccontextmanager
    async def _http(self):
        if self._client is not None:
            yield self._client
        else:
            from app.channels import pool

            yield pool.shared(TIMEOUT)

    def _auth(self) -> tuple[str, str]:
        return settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN

    async def send(self, to: str, text: str, buttons: Optional[list[tuple[str, str]]] = None) -> bool:
        body = text
        if buttons:
            remember_choices(to, buttons)
            choices = "\n".join(f"{i + 1}. {label}" for i, (label, _data) in enumerate(buttons[:MAX_BUTTONS]))
            body = f"{text}\n\n{choices}\n(Reply with the number)"
        data = {"From": f"whatsapp:{settings.TWILIO_WHATSAPP_NUMBER}", "To": f"whatsapp:+{to}", "Body": body[:BODY_LIMIT]}
        try:
            async with self._http() as client:
                res = await client.post(f"{API}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json", data=data, auth=self._auth())
            if res.status_code >= 300:
                logger.warning("Twilio WhatsApp send was refused: %s %s", res.status_code, _reason(res))
            return res.status_code < 300
        except Exception as exc:  # noqa: BLE001 - a failed send is logged, never raised
            logger.warning("Twilio WhatsApp send failed: %s", type(exc).__name__)
            return False

    async def fetch_media(self, media: dict[str, Any]) -> tuple[bytes, str]:
        url = str(media.get("url") or "")
        if not url.startswith("https://"):
            raise FileNotFoundError(url[:40])
        async with self._http() as client:
            res = await client.get(url, auth=self._auth(), follow_redirects=True)
        res.raise_for_status()
        return res.content, "voice.ogg"


def _reason(res: httpx.Response) -> str:
    """Twilio's own error code and message for a refused send, which carry nothing about the person."""
    try:
        body = res.json()
        return f"{body.get('code')} {str(body.get('message'))[:120]}"
    except Exception:  # noqa: BLE001
        return ""
