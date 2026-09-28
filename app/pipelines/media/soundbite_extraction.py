"""Real soundbite extraction — trims a real span out of a real master
recording and measures it, for the Batch Approval Queue (previously
mock-only, see BatchApprovalQueue.tsx's own "Phase 2 Reserved" comment).

Confidence/status are real, measured values, never an AI-labeled guess:
peak clipping and how far the clip's own loudness sits from a broadcast
target, the same real metrics audio_cleanup.py already uses elsewhere in
this codebase.
"""

import io

import numpy as np
import pyloudnorm as pyln
import soundfile as sf

from app.models.audio_asset import SoundbiteStatus

TARGET_LUFS = -16.0
LUFS_TOLERANCE = 4.0  # within this many LU of target still counts "ready"
CLIP_PEAK_THRESHOLD = 0.98


class SoundbiteExtractionError(Exception):
    """A message that is safe to show the member."""


def trim_span(master_bytes: bytes, start_s: float, end_s: float) -> bytes:
    """Real sample-accurate trim of the real master audio. Raises on a
    span that doesn't actually fit the real decoded audio."""
    try:
        data, sr = sf.read(io.BytesIO(master_bytes), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001
        raise SoundbiteExtractionError("Could not read the master recording to trim it.") from exc

    duration = len(data) / sr
    if not (0 <= start_s < end_s <= duration + 0.5):
        raise SoundbiteExtractionError("That span doesn't fit this recording's real length.")

    start_i, end_i = int(start_s * sr), min(int(end_s * sr), len(data))
    clip = data[start_i:end_i]
    if len(clip) < sr * 0.3:
        raise SoundbiteExtractionError("That span is too short to be a real soundbite.")

    buf = io.BytesIO()
    sf.write(buf, clip, sr, format="WAV")
    return buf.getvalue()


def evaluate_quality(clip_bytes: bytes) -> tuple[SoundbiteStatus, int, "str | None", "float | None"]:
    """Returns (status, confidence 0-100, flag_message, measured_lufs) —
    all derived from real measurements on the real trimmed clip, not a
    fabricated number."""
    data, sr = sf.read(io.BytesIO(clip_bytes), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)

    peak = float(np.abs(mono).max()) if len(mono) else 0.0
    clipping = peak >= CLIP_PEAK_THRESHOLD

    measured_lufs = None
    lufs_delta = None
    try:
        meter = pyln.Meter(sr)
        measured_lufs = float(meter.integrated_loudness(data))
        if measured_lufs > -70.0:  # pyloudnorm returns -inf-ish for near-silence
            lufs_delta = abs(measured_lufs - TARGET_LUFS)
    except Exception:  # noqa: BLE001
        pass

    issues: list[str] = []
    confidence = 100
    if clipping:
        issues.append("audio peaks are clipping")
        confidence -= 30
    if lufs_delta is not None and lufs_delta > LUFS_TOLERANCE:
        issues.append(f"loudness is {round(lufs_delta, 1)} LU off broadcast target")
        confidence -= 20
    if lufs_delta is None:
        issues.append("mostly silence, little real speech in this span")
        confidence -= 40

    confidence = max(0, min(100, confidence))
    status = SoundbiteStatus.READY if not issues else SoundbiteStatus.NEEDS_ATTENTION
    flag_message = "; ".join(issues).capitalize() + "." if issues else None
    return status, confidence, flag_message, measured_lufs
