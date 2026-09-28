"""Member-controlled cleanup of a recording: the dials on the Cleanup tab.

Everything here is real signal processing on the decoded audio (numpy, scipy,
noisereduce, pyloudnorm), with no ffmpeg. It is separate from
`audio_enhance.enhance_audio`, which is the fixed cleanup that runs once on
upload; this applies whatever the member chose, on demand.

Echo/room-noise reduction is NOT here on purpose — there is no dependable
local method for it. It is a real external call instead
(app.pipelines.media.echo_reduction.reduce_echo, ElevenLabs Audio
Isolation), orchestrated by the cleanup endpoint before this module's own
local DSP chain runs, not by this module itself.

Time cuts (silence trimming and filler-word removal) change the length of the
audio, so the word timings of the transcript are remapped to match.
"""

import io
from typing import Optional

import noisereduce as nr
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from pydantic import BaseModel, Field
from scipy.signal import butter, sosfilt, sosfiltfilt

MAX_SECONDS = 30 * 60

_FILLERS = {"um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "uhm", "mm"}


class CleanupSettings(BaseModel):
    """Every field is optional; a field left out means "don't do this"."""
    noise_reduction: Optional[float] = Field(default=None, ge=0.05, le=1.0)      # strength
    target_lufs: Optional[float] = Field(default=None, ge=-30.0, le=-5.0)         # loudness
    highpass_hz: Optional[int] = Field(default=None, ge=40, le=400)               # rumble
    lowpass_hz: Optional[int] = Field(default=None, ge=3000, le=16000)            # hiss
    compressor: Optional[float] = Field(default=None, ge=0.05, le=1.0)            # even out volume
    de_esser: Optional[float] = Field(default=None, ge=0.05, le=1.0)              # harsh "s" sounds
    silence_trim_s: Optional[float] = Field(default=None, ge=0.3, le=2.0)         # shorten gaps longer than this
    crossfade_ms: int = Field(default=12, ge=4, le=50)                            # smooths every cut
    remove_fillers: bool = False                                                  # needs a transcript
    mouth_click_removal: Optional[float] = Field(default=None, ge=0.05, le=1.0)   # de-click strength
    room_tone_fill: bool = False                                                  # needs a real cut to fill
    remove_echo: bool = False                                                     # real external call, see module docstring

    def is_empty(self) -> bool:
        return not any((
            self.noise_reduction, self.target_lufs, self.highpass_hz, self.lowpass_hz,
            self.compressor, self.de_esser, self.silence_trim_s, self.remove_fillers,
            self.mouth_click_removal, self.room_tone_fill, self.remove_echo,
        ))


class CleanupError(ValueError):
    """A message that is safe to show the member."""


def _decode(audio_bytes: bytes) -> tuple[np.ndarray, int]:
    try:
        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001
        raise CleanupError("This file's format can't be edited here. Upload a WAV, MP3 or OGG file.") from exc
    if data.shape[0] / sr > MAX_SECONDS:
        raise CleanupError("This recording is longer than 30 minutes, which is too long to clean up in one go.")
    return data, sr  # (samples, channels)


# ── time cuts ────────────────────────────────────────────────────────────────

def _silence_cuts(mono: np.ndarray, sr: int, keep_s: float) -> list[tuple[float, float]]:
    """Spans (seconds) to remove from silent gaps longer than keep_s, leaving
    half of keep_s on each side so speech doesn't run together."""
    frame = int(sr * 0.02)
    if frame < 1 or len(mono) < frame * 4:
        return []
    n = len(mono) // frame
    rms = np.sqrt(np.mean(mono[: n * frame].reshape(n, frame) ** 2, axis=1))
    floor = float(np.percentile(rms, 10))
    threshold = max(floor * 3.0, 0.004)
    quiet = rms < threshold

    cuts: list[tuple[float, float]] = []
    i = 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        start_s, end_s = i * frame / sr, j * frame / sr
        if end_s - start_s > keep_s:
            cuts.append((start_s + keep_s / 2, end_s - keep_s / 2))
        i = j
    return cuts


def _filler_cuts(transcript: list[dict]) -> list[tuple[float, float]]:
    cuts = []
    for w in transcript:
        token = "".join(ch for ch in w["word"].lower() if ch.isalpha())
        if token in _FILLERS and w["end_s"] > w["start_s"]:
            cuts.append((float(w["start_s"]), float(w["end_s"])))
    return cuts


def _merge(cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(cuts):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _extract_room_tone(mono: np.ndarray, sr: int, seconds: float = 1.0) -> np.ndarray:
    """The real quietest contiguous stretch of THIS recording — literal
    captured ambience, not a generated hiss standing in for one."""
    frame = max(1, int(sr * 0.05))
    window = max(1, int(seconds / 0.05))
    n = len(mono) // frame
    if n < window:
        return np.zeros(int(sr * seconds), dtype="float32")
    rms = np.sqrt(np.mean(mono[: n * frame].reshape(n, frame) ** 2, axis=1))
    windowed = np.convolve(rms, np.ones(window) / window, mode="valid")
    best_start = int(np.argmin(windowed)) * frame
    return mono[best_start: best_start + window * frame].copy()


def _seam_positions(cuts: list[tuple[float, float]], pieces: list[np.ndarray]) -> list[int]:
    """Sample offsets into the concatenated, cut-down audio where two
    once-separate pieces now meet — where a hard join would otherwise be."""
    positions = []
    cursor = 0
    for p in pieces[:-1]:
        cursor += len(p)
        positions.append(cursor)
    return positions


def _apply_cuts(
    data: np.ndarray, sr: int, cuts: list[tuple[float, float]], crossfade_ms: int,
    room_tone: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Removes each span and joins the neighbours with a short equal-power
    crossfade, so a cut never clicks. With `room_tone` given, also lays a
    touch of the recording's own real ambience across each seam so the join
    doesn't read as a sudden dead patch."""
    if not cuts:
        return data
    xf = max(1, int(sr * crossfade_ms / 1000))
    pieces: list[np.ndarray] = []
    cursor = 0
    for start, end in cuts:
        s, e = int(start * sr), int(end * sr)
        if s > cursor:
            pieces.append(data[cursor:s])
        cursor = max(cursor, e)
    pieces.append(data[cursor:])
    pieces = [p for p in pieces if len(p)]
    if not pieces:
        return data[:1]

    seams = _seam_positions(cuts, pieces) if room_tone is not None else []

    out = pieces[0]
    fade_in = np.sin(np.linspace(0, np.pi / 2, xf, dtype="float32"))[:, None]
    fade_out = np.cos(np.linspace(0, np.pi / 2, xf, dtype="float32"))[:, None]
    for nxt in pieces[1:]:
        n = min(xf, len(out), len(nxt))
        if n < 2:
            out = np.concatenate([out, nxt])
            continue
        blend = out[-n:] * fade_out[:n] + nxt[:n] * fade_in[:n]
        out = np.concatenate([out[:-n], blend, nxt[n:]])

    if room_tone is not None and len(room_tone) and seams:
        pad = xf * 3
        tone = room_tone if room_tone.ndim > 1 else room_tone[:, None]
        window = np.hanning(pad * 2).astype("float32")[:, None] if pad > 1 else np.ones((1, 1), dtype="float32")
        for pos in seams:
            lo, hi = max(0, pos - pad), min(len(out), pos + pad)
            span = hi - lo
            if span < 2:
                continue
            reps = int(np.ceil(span / len(tone))) if len(tone) else 1
            patch = np.tile(tone, (max(reps, 1), 1))[:span]
            out[lo:hi] = out[lo:hi] + patch * window[: span]
    return out


def _remap_transcript(transcript: list[dict], cuts: list[tuple[float, float]]) -> list[dict]:
    """Moves every word to where it now sits and drops words that were cut."""
    if not cuts:
        return transcript
    remapped = []
    for w in transcript:
        mid = (w["start_s"] + w["end_s"]) / 2
        if any(start <= mid <= end for start, end in cuts):
            continue

        def shift(t: float) -> float:
            removed = 0.0
            for start, end in cuts:
                if end <= t:
                    removed += end - start
                elif start < t:
                    removed += t - start
            return max(0.0, t - removed)

        remapped.append({**w, "start_s": round(shift(w["start_s"]), 3), "end_s": round(shift(w["end_s"]), 3)})
    return remapped


# ── filters and dynamics ─────────────────────────────────────────────────────

def _bandpass_sos(sr: int, low: float, high: float):
    return butter(4, [low, min(high, sr / 2 - 100)], btype="band", fs=sr, output="sos")


def _de_ess(channel: np.ndarray, sr: int, amount: float) -> np.ndarray:
    """Turns down the 5-9 kHz band only while it is loud, which is what a
    harsh "s" is."""
    if sr < 16000:
        return channel
    sos = _bandpass_sos(sr, 5000, 9000)
    band = sosfilt(sos, channel)
    env = sosfilt(butter(2, 60, btype="low", fs=sr, output="sos"), np.abs(band))
    threshold = max(float(np.percentile(env, 90)) * 0.6, 1e-4)
    reduction = np.clip((env - threshold) / threshold, 0.0, 1.0) * amount
    return (channel - band * reduction).astype("float32")


def _compress(channel: np.ndarray, sr: int, amount: float) -> np.ndarray:
    """Feed-forward compressor: quiet parts are left alone, loud parts are
    pulled toward the threshold. `amount` sets the ratio from 1.5:1 to 6:1."""
    ratio = 1.5 + amount * 4.5
    threshold_db = -20.0
    env = sosfilt(butter(2, 20, btype="low", fs=sr, output="sos"), np.abs(channel))
    env_db = 20 * np.log10(np.maximum(env, 1e-6))
    over = np.maximum(env_db - threshold_db, 0.0)
    gain_db = -over * (1 - 1 / ratio)
    return (channel * (10 ** (gain_db / 20))).astype("float32")


def _declick(channel: np.ndarray, sr: int, amount: float) -> np.ndarray:
    """Finds short, sharp discontinuities — a mouth click or lip smack jumps
    far outside its own local envelope for a few milliseconds — and repairs
    each by interpolating across it. A click's energy spreads across the
    whole spectrum, not one band, so this looks at the waveform itself
    rather than filtering a frequency range."""
    # sosfiltfilt (zero-phase, no warm-up transient) rather than sosfilt —
    # a one-sided filter's own startup ramp reads as a false spike at the
    # very start of the file otherwise.
    envelope = sosfiltfilt(butter(2, 40, btype="low", fs=sr, output="sos"), np.abs(channel))
    local = np.maximum(envelope, 1e-4)
    spike = np.abs(channel) / local
    threshold = max(3.0, 8.0 - amount * 5.0)  # stronger amount catches smaller spikes too
    flagged = spike > threshold

    max_run = max(1, int(sr * 0.006))  # a real click is a few ms; a longer run is just loud speech
    # The very edges of the buffer have no full neighbour context to repair
    # from, and a signal that happens to start or end near zero can look
    # like a spike by this same ratio test for reasons that have nothing to
    # do with a real click — leave the edges alone rather than risk a
    # false repair there.
    edge = max_run * 2
    flagged[:edge] = False
    flagged[-edge:] = False
    out = channel.copy()
    n = len(flagged)
    i = 0
    while i < n:
        if not flagged[i]:
            i += 1
            continue
        j = i
        while j < n and flagged[j]:
            j += 1
        if j - i <= max_run:
            lo, hi = max(0, i - 2), min(n, j + 2)
            if lo < i and hi > j:
                out[i:j] = np.interp(np.arange(i, j), [lo, hi - 1], [out[lo], out[hi - 1]])
        i = j
    return out.astype("float32")


def apply_cleanup(
    audio_bytes: bytes, settings: CleanupSettings, transcript: list[dict],
) -> tuple[bytes, list[dict], dict]:
    """Returns (wav_bytes, transcript_after, what_was_applied)."""
    if settings.is_empty():
        raise CleanupError("Turn on at least one cleanup option first.")
    if settings.remove_fillers and not transcript:
        raise CleanupError("Filler-word removal needs a transcript, and this recording doesn't have one.")

    data, sr = _decode(audio_bytes)
    applied: dict = {}

    # 1. Cuts: fillers and long silences. Times are in the original audio.
    cuts: list[tuple[float, float]] = []
    if settings.remove_fillers:
        found = _filler_cuts(transcript)
        applied["fillers_removed"] = len(found)
        cuts += found
    if settings.silence_trim_s:
        found = _silence_cuts(data.mean(axis=1), sr, settings.silence_trim_s)
        applied["silence_trim_s"] = settings.silence_trim_s
        applied["gaps_shortened"] = len(found)
        cuts += found
    cuts = _merge(cuts)
    if cuts:
        applied["crossfade_ms"] = settings.crossfade_ms
        seconds_removed = sum(e - s for s, e in cuts)
        applied["seconds_removed"] = round(seconds_removed, 2)
        room_tone = None
        if settings.room_tone_fill:
            room_tone = _extract_room_tone(data.mean(axis=1), sr)
            applied["room_tone_fill"] = True
        data = _apply_cuts(data, sr, cuts, settings.crossfade_ms, room_tone=room_tone)
        transcript = _remap_transcript(transcript, cuts)
    elif settings.room_tone_fill:
        raise CleanupError("Room tone fill only does something once a real cut is made — turn on Silence Trimmer or Remove Filler Words too.")

    # 2. Per-channel processing.
    channels = []
    for ch in range(data.shape[1]):
        x = data[:, ch].astype("float32")
        if settings.highpass_hz:
            x = sosfiltfilt(butter(4, settings.highpass_hz, btype="high", fs=sr, output="sos"), x).astype("float32")
        if settings.lowpass_hz and settings.lowpass_hz < sr / 2 - 100:
            x = sosfiltfilt(butter(4, settings.lowpass_hz, btype="low", fs=sr, output="sos"), x).astype("float32")
        if settings.noise_reduction:
            x = nr.reduce_noise(y=x, sr=sr, prop_decrease=settings.noise_reduction).astype("float32")
        if settings.de_esser:
            x = _de_ess(x, sr, settings.de_esser)
        if settings.compressor:
            x = _compress(x, sr, settings.compressor)
        if settings.mouth_click_removal:
            x = _declick(x, sr, settings.mouth_click_removal)
        channels.append(x)
    out = np.stack(channels, axis=1)
    for key in ("highpass_hz", "lowpass_hz", "noise_reduction", "de_esser", "compressor", "mouth_click_removal"):
        if getattr(settings, key):
            applied[key] = getattr(settings, key)

    # 3. Loudness last, so nothing before it undoes it.
    if settings.target_lufs is not None:
        meter = pyln.Meter(sr)
        mono_for_meter = out if out.shape[1] > 1 else out[:, 0]
        try:
            measured = meter.integrated_loudness(mono_for_meter)
            if np.isfinite(measured):
                out = pyln.normalize.loudness(out, measured, settings.target_lufs)
                applied["target_lufs"] = settings.target_lufs
                applied["measured_loudness_before_lufs"] = round(float(measured), 2)
        except ValueError:
            raise CleanupError("This recording is too short to measure loudness.")

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.99:  # never hand back a clipped file
        out = out * (0.99 / peak)

    buf = io.BytesIO()
    sf.write(buf, out if out.shape[1] > 1 else out[:, 0], sr, format="WAV")
    return buf.getvalue(), transcript, applied
