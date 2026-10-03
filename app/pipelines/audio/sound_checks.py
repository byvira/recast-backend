"""
Sound checks for a recording: three things a listener notices at once, measured from the audio itself.

  loudness       how loud it is overall (integrated loudness, in LUFS). Spoken word is normally published near -16 LUFS; a
                 recording well under -20 sounds faint next to others, well over -12 sounds harsh.
  peaks          whether the loudest moment touches the top and may distort (clipping).
  silences       gaps of three seconds or more in the middle of the recording, which listeners read as a dropout.

They are advice, shown as good or "worth a look" with a plain sentence. Nothing here blocks anything. The thresholds are the
common ones for spoken-word audio, not a promise about any one platform.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
from typing import Optional

GOOD_LOUDNESS_RANGE = (-20.0, -12.0)   # LUFS
CLIP_PEAK_DB = -0.5                    # dBFS at or above this may distort
LONG_SILENCE_S = 3.0
SILENCE_FLOOR_DB = -40


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def parse_report(report: str) -> dict:
    """Pull the numbers out of ffmpeg's text report. Anything missing stays None."""
    metrics: dict = {"loudness_lufs": None, "peak_db": None, "silences": []}
    block = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", report, re.DOTALL)
    if block:
        try:
            data = json.loads(block.group(0))
            value = float(data.get("input_i"))
            metrics["loudness_lufs"] = value if value > -70 else None   # -inf (silence) comes back as a very low number
        except (ValueError, TypeError):
            pass
    peak = re.findall(r"max_volume:\s*(-?[\d.]+) dB", report)
    if peak:
        metrics["peak_db"] = float(peak[-1])
    metrics["silences"] = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", report)]
    # False when ffmpeg could not read the file at all (then nothing, including "no silences", can be said)
    metrics["measured"] = metrics["loudness_lufs"] is not None or metrics["peak_db"] is not None
    return metrics


def judge(metrics: dict) -> list[dict]:
    """The three checks as {id, label, status, message}; status is "good" or "attention", or "unknown" when it could not be read."""
    checks = []
    lufs = metrics.get("loudness_lufs")
    low, high = GOOD_LOUDNESS_RANGE
    if lufs is None:
        checks.append({"id": "loudness", "label": "Loudness", "status": "unknown", "message": "Couldn't measure the loudness."})
    elif lufs < low:
        checks.append({"id": "loudness", "label": "Loudness", "status": "attention",
                       "message": f"Quiet ({lufs:.0f} LUFS). Spoken word usually sits near -16, so this may sound faint. Raise the level in Cleanup."})
    elif lufs > high:
        checks.append({"id": "loudness", "label": "Loudness", "status": "attention",
                       "message": f"Loud ({lufs:.0f} LUFS). Spoken word usually sits near -16, so this may sound harsh. Lower the level in Cleanup."})
    else:
        checks.append({"id": "loudness", "label": "Loudness", "status": "good", "message": f"Comfortable ({lufs:.0f} LUFS)."})

    peak = metrics.get("peak_db")
    if peak is None:
        checks.append({"id": "peaks", "label": "Peaks", "status": "unknown", "message": "Couldn't measure the peaks."})
    elif peak >= CLIP_PEAK_DB:
        checks.append({"id": "peaks", "label": "Peaks", "status": "attention",
                       "message": "The loudest moment reaches the top, so it may distort. Lower the level a little."})
    else:
        checks.append({"id": "peaks", "label": "Peaks", "status": "good", "message": "No clipping."})

    silences = [s for s in metrics.get("silences", []) if s >= LONG_SILENCE_S]
    if not metrics.get("measured"):
        checks.append({"id": "silences", "label": "Silences", "status": "unknown", "message": "Couldn't check for gaps."})
    elif silences:
        longest = max(silences)
        word = "gap" if len(silences) == 1 else "gaps"
        checks.append({"id": "silences", "label": "Silences", "status": "attention",
                       "message": f"{len(silences)} {word} of {LONG_SILENCE_S:.0f} seconds or more (longest {longest:.0f}s). Trim them in Cleanup."})
    else:
        checks.append({"id": "silences", "label": "Silences", "status": "good", "message": "No long gaps."})
    return checks


def _measure_blocking(audio_bytes: bytes) -> Optional[dict]:
    fd, path = tempfile.mkstemp(suffix=".audio")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(audio_bytes)
        result = subprocess.run(
            [_ffmpeg_exe(), "-hide_banner", "-nostats", "-i", path, "-af",
             f"volumedetect,silencedetect=noise={SILENCE_FLOOR_DB}dB:d={LONG_SILENCE_S:g},loudnorm=print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        return parse_report(result.stderr or "")
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def run_sound_checks(audio_bytes: bytes) -> list[dict]:
    """Measure a recording and judge it. Never raises: a recording that cannot be read gives three "unknown" checks."""
    metrics = await asyncio.to_thread(_measure_blocking, audio_bytes)
    return judge(metrics or {"loudness_lufs": None, "peak_db": None, "silences": []})
