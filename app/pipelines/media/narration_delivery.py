"""How a narration is delivered and finished, as choices a member can make for a recording.

Delivery style sets the voice's tone, pauses and energy. Mastering is the finishing pass on the file: studio mastering softens harsh
"s" sounds and evens out the volume, broadcast mastering compresses harder for a consistent, radio-style level. Both are plain,
real changes: the style goes into the voice settings the speech is made with, and mastering is real audio processing."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.pipelines.media.audio_cleanup import CleanupError, CleanupSettings, apply_cleanup

logger = logging.getLogger(__name__)

#: Fields of the member's voice settings that each delivery style sets.
_DELIVERY = {
    "founder": {"emotional_tone": "authoritative", "pause_cadence": "natural", "vocal_energy": 75},
    "casual": {"emotional_tone": "conversational", "pause_cadence": "natural", "vocal_energy": 70},
    "keynote": {"emotional_tone": "visionary", "pause_cadence": "tight", "vocal_energy": 90},
}

_MASTERING = {
    "studio": CleanupSettings(de_esser=0.5, compressor=0.4, target_lufs=-16.0),
    "broadcast": CleanupSettings(de_esser=0.3, compressor=0.8, target_lufs=-16.0),
}


def settings_for(style: str) -> dict:
    """The voice-settings changes for a delivery style (nothing for one that is not known)."""
    return dict(_DELIVERY.get(style, {}))


async def master(audio_bytes: bytes, preset: str) -> Optional[bytes]:
    """The recording with the mastering preset applied, as WAV bytes, or None when it could not be processed."""
    settings = _MASTERING.get(preset)
    if settings is None:
        return None
    try:
        out, _, _ = await asyncio.to_thread(apply_cleanup, audio_bytes, settings, [])
        return out
    except (CleanupError, Exception) as exc:  # noqa: BLE001 - a failed finish must never lose the narration
        logger.warning("Narration mastering %s skipped: %s", preset, exc)
        return None
