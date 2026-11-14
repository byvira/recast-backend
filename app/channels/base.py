"""The shapes every channel shares: a normalised incoming message and the small interface an adapter fills in."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

KINDS = ("text", "voice", "image", "button", "other")


@dataclass(frozen=True)
class Inbound:
    """One message from a chat, the same shape whichever service it came from."""

    channel: str
    #: the service's own id for the chat (a phone number, a Telegram chat id); never stored as it is, see link.py
    sender: str
    message_id: str
    kind: str = "text"
    text: str = ""
    #: for a voice note or a picture: what the adapter needs to fetch the file
    media: Optional[dict[str, Any]] = None
    #: for a button press: what the button carried
    payload: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Adapter(Protocol):
    """What the engine needs from a service. Everything else is the engine's."""

    name: str

    async def send(self, to: str, text: str, buttons: Optional[list[tuple[str, str]]] = None) -> bool:
        """Send a short message, with up to three (label, payload) buttons. True when the service accepted it. Never raises."""

    async def fetch_media(self, media: dict[str, Any]) -> tuple[bytes, str]:
        """The bytes and a file name for a voice note or picture. Raises on failure; the engine tells the person."""
