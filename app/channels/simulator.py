"""A stand-in chat service for tests and for trying the engine without Meta or Telegram.

It records what the engine sends, so a test can read the replies, and hands back a chosen file for a voice note. It is also what the
signed test route in `app/api/channels.py` uses when `CHANNELS_SIMULATOR` is on (never in production)."""
from __future__ import annotations

from typing import Any, Optional


class SimulatorAdapter:
    name = "simulator"

    def __init__(self, media: Optional[dict[str, bytes]] = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.media = media or {}

    async def send(self, to: str, text: str, buttons: Optional[list[tuple[str, str]]] = None) -> bool:
        self.sent.append({"to": to, "text": text, "buttons": buttons or []})
        return True

    async def fetch_media(self, media: dict[str, Any]) -> tuple[bytes, str]:
        ref = str(media.get("id") or "")
        if ref not in self.media:
            raise FileNotFoundError(ref)
        return self.media[ref], "voice.ogg"

    @property
    def last(self) -> dict[str, Any]:
        return self.sent[-1]
