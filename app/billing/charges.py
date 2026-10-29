"""Small helpers for charge points that are measured in minutes or words, so every route charges them the same way."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from app.billing import credits


def words_in(text: str) -> int:
    return len((text or "").split())


def minutes(seconds: Optional[float], fallback_s: float = 60.0) -> float:
    """Whole work in minutes, never below one second's worth so a real piece of work is never free."""
    value = float(seconds) if seconds else float(fallback_s)
    return max(value, 1.0) / 60.0


async def voiced(
    workspace_id: str, user_id: Optional[str], action: str, text: str, make: Callable[[], Awaitable[Any]], *,
    step: Optional[str] = None, wpm: Optional[float] = None,
) -> Any:
    """Voice `text` with `make()` and charge it by the minute (narration or dialogue). The credits for the expected length are set
    aside first; the real length of the audio is what is charged, and nothing when no audio came back.

    `make` returns the provider's answer: an object with `.audio` (timed speech), raw bytes, or None."""
    from app.pipelines.media import duration as spoken

    estimate_s = spoken.estimate_seconds(text, wpm)
    async with credits.spend(workspace_id, user_id, action, minutes(estimate_s), step=step or action) as hold:
        speech = await make()
        if not speech:
            hold.nothing_made = True
        else:
            audio = getattr(speech, "audio", speech)
            real_s = spoken.audio_duration_seconds(audio) if isinstance(audio, (bytes, bytearray)) else None
            hold.actual_units = minutes(real_s, estimate_s)
    return speech
