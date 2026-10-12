"""Whether a recording is small enough to process in memory, worked out from its header before any of it is decoded.

Cleaning or mixing audio decodes it to 32-bit numbers and makes several working copies. Measured on this server's code: about
16 bytes of memory per sample per channel at the peak (a 3 minute stereo recording at 44.1 kHz peaks at about 230 MB above what
the server already holds). The server has 512 MB, so only a few minutes of stereo audio can be worked on at once. Longer
recordings still upload, play, transcribe and publish; only the cleanup and mixing steps are refused, with a plain message.
Raise `AUDIO_WORK_BUDGET_MB` (with a bigger server) to allow longer recordings.
"""
from __future__ import annotations

import io
import os
from typing import Optional

import soundfile as sf

from app.core import limits

#: Memory the audio work may use at its peak.
WORK_BUDGET_BYTES = limits.AUDIO_WORK_BUDGET_BYTES
#: Peak bytes used per sample per channel (4-byte samples, about four copies alive at once).
BYTES_PER_SAMPLE_AT_PEAK = 16


def seconds_allowed(samplerate: int, channels: int, budget_bytes: Optional[int] = None) -> float:
    return (budget_bytes or WORK_BUDGET_BYTES) / (BYTES_PER_SAMPLE_AT_PEAK * max(1, samplerate) * max(1, channels))


def check_fits(audio_bytes: bytes, *, budget_bytes: Optional[int] = None) -> Optional[str]:
    """None when the recording fits; otherwise a plain sentence saying how long a recording this server can work on.
    A file whose header cannot be read returns None (decoding reports that problem itself)."""
    try:
        info = sf.info(io.BytesIO(audio_bytes))
    except Exception:  # noqa: BLE001
        return None
    if not info.samplerate or not info.frames:
        return None
    seconds = info.frames / info.samplerate
    allowed = seconds_allowed(info.samplerate, info.channels, budget_bytes)
    if seconds <= allowed:
        return None
    return (
        f"This recording is {seconds / 60:.0f} minutes long. Cleaning up and mixing work on recordings up to about "
        f"{max(1, int(allowed // 60))} minute{'s' if allowed >= 120 else ''} here. You can still play, transcribe and publish it."
    )
