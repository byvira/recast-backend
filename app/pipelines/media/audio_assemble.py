"""Assembles a finished episode from the recording plus the pieces a member
has set up: an intro clip, a sponsor read, an outro clip, a music bed, a
transition sound dropped into real pauses, a brand signature at the very
start, stereo widening on the music, and warmth on the voice.

Real mixing on decoded audio (numpy/scipy/soundfile), no ffmpeg:
  [signature] + intro + [voice, with the sponsor read dropped in at a chosen
  second and a transition sound at real pauses] + outro
and, optionally, a music bed that runs under the voice part and dips while
someone is speaking (ducking), so the voice always sits on top.

The result is a new file. The recording itself is never changed.
"""

import io
from typing import Optional

import numpy as np
import soundfile as sf
from pydantic import BaseModel, Field
from scipy.signal import butter, resample_poly, sosfilt

MAX_SECONDS = 30 * 60
_JOIN_MS = 30  # short equal-power blend wherever two pieces meet


class AssembleError(ValueError):
    """A message that is safe to show the member."""


class AssemblePlan(BaseModel):
    use_intro: bool = False
    use_outro: bool = False
    sponsor_at_s: Optional[float] = Field(default=None, ge=0)     # None = no sponsor read
    music_bed_id: Optional[str] = None
    # How much quieter the bed sits than the voice, in dB (a voice/music
    # balance of 85/15 is about -15). Then how far it dips while speaking.
    music_level_db: float = Field(default=-15.0, ge=-40.0, le=-3.0)
    ducking_db: float = Field(default=-12.0, ge=-30.0, le=0.0)
    # How much wider the music bed's stereo image is: 1.0 is unchanged,
    # >1 widens. The voice, being mono, is never affected — it stays centred.
    stereo_width: Optional[float] = Field(default=None, ge=0.8, le=2.0)
    # Soft tape-style saturation on the voice only.
    voice_warmth: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Drops the kit's transition sound into real pauses in the voice — found
    # from actual gaps in the recording's own transcript, never invented.
    use_transition_sfx: bool = False
    max_transitions: int = Field(default=6, ge=1, le=20)
    use_brand_signature: bool = False


def _decode(data: bytes, what: str) -> tuple[np.ndarray, int]:
    try:
        audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001
        raise AssembleError(f"The {what} isn't a file that can be mixed. Use a WAV, MP3 or OGG file.") from exc
    if audio.shape[0] == 0:
        raise AssembleError(f"The {what} is empty.")
    return audio, sr


def _match(audio: np.ndarray, sr: int, target_sr: int, channels: int) -> np.ndarray:
    """Same sample rate and channel count as the voice."""
    if sr != target_sr:
        g = np.gcd(sr, target_sr)
        audio = np.stack(
            [resample_poly(audio[:, c], target_sr // g, sr // g) for c in range(audio.shape[1])], axis=1,
        ).astype("float32")
    if audio.shape[1] == channels:
        return audio
    if channels == 1:
        return audio.mean(axis=1, keepdims=True).astype("float32")
    return np.repeat(audio[:, :1], channels, axis=1) if audio.shape[1] == 1 else audio[:, :channels]


def _join(pieces: list[np.ndarray], sr: int) -> np.ndarray:
    """Concatenate with a tiny crossfade at every seam so nothing clicks."""
    pieces = [p for p in pieces if len(p)]
    xf = int(sr * _JOIN_MS / 1000)
    out = pieces[0]
    for nxt in pieces[1:]:
        n = min(xf, len(out), len(nxt))
        if n < 2:
            out = np.concatenate([out, nxt])
            continue
        fade_in = np.sin(np.linspace(0, np.pi / 2, n, dtype="float32"))[:, None]
        fade_out = np.cos(np.linspace(0, np.pi / 2, n, dtype="float32"))[:, None]
        out = np.concatenate([out[:-n], out[-n:] * fade_out + nxt[:n] * fade_in, nxt[n:]])
    return out


def _duck_gain(voice: np.ndarray, sr: int, ducking_db: float) -> np.ndarray:
    """1.0 where the voice is quiet, the ducked gain where it speaks, with
    smooth attack and release so the bed swells back rather than snapping."""
    mono = np.abs(voice).mean(axis=1)
    env = sosfilt(butter(2, 8, btype="low", fs=sr, output="sos"), mono)
    peak = float(np.percentile(env, 95)) or 1e-6
    speaking = np.clip((env - 0.08 * peak) / (0.25 * peak), 0.0, 1.0)  # 0 = silence, 1 = speech
    # Attack fast, release slow, by smoothing the "speaking" curve.
    attack = sosfilt(butter(2, 12, btype="low", fs=sr, output="sos"), speaking)
    release = sosfilt(butter(2, 2.5, btype="low", fs=sr, output="sos"), speaking)
    smooth = np.clip(np.maximum(attack, release), 0.0, 1.0)
    duck = 10 ** (ducking_db / 20)
    return (1.0 - smooth * (1.0 - duck)).astype("float32")


def _under_voice(voice: np.ndarray, bed: np.ndarray, sr: int, plan: AssemblePlan) -> np.ndarray:
    """Loops or trims the bed to the voice's length, sets its level, widens
    it, ducks it under speech and fades it in and out."""
    reps = int(np.ceil(len(voice) / len(bed)))
    looped = np.tile(bed, (reps, 1))[: len(voice)]
    looped = looped / max(float(np.max(np.abs(looped))), 1e-6) * float(np.max(np.abs(voice)) or 0.5)
    if plan.stereo_width and plan.stereo_width != 1.0 and looped.shape[1] == 2:
        looped = _widen_stereo(looped, plan.stereo_width)
    level = 10 ** (plan.music_level_db / 20)
    gain = _duck_gain(voice, sr, plan.ducking_db)[:, None]
    fade = min(int(sr * 1.5), len(voice) // 4)
    if fade > 1:
        ramp = np.linspace(0, 1, fade, dtype="float32")
        gain[:fade, 0] *= ramp
        gain[-fade:, 0] *= ramp[::-1]
    return voice + looped * level * gain


def _widen_stereo(stereo: np.ndarray, width: float) -> np.ndarray:
    """Real mid/side widening: turn the stereo pair into a shared centre
    (mid) and the difference between channels (side), scale the side by
    `width`, then convert back. A mono voice has no side content at all, so
    this only ever affects material that is genuinely stereo to begin with —
    a mono recording widened this way stays exactly as it was."""
    left, right = stereo[:, 0], stereo[:, 1]
    mid = (left + right) / 2
    side = (left - right) / 2 * width
    widened = np.stack([mid + side, mid - side], axis=1)
    peak = float(np.max(np.abs(widened)))
    if peak > 0.99:  # only guards against real clipping, not the widening itself
        widened *= 0.99 / peak
    return widened.astype("float32")


def _warm(mono: np.ndarray, amount: float) -> np.ndarray:
    """Soft tape-style saturation (a tanh waveshaper — the standard, real
    technique for adding harmonic warmth) plus a gentle low-end tilt, in
    place of a true analog circuit."""
    drive = 1.0 + amount * 4.0
    shaped = np.tanh(mono * drive) / np.tanh(drive)
    tilted = shaped + 0.15 * amount * sosfilt(butter(2, 300, btype="low", fs=48000, output="sos"), shaped)
    peak = float(np.max(np.abs(tilted))) or 1.0
    source_peak = float(np.max(np.abs(mono))) or 1.0
    return (tilted * min(1.0, source_peak / peak * 1.05)).astype("float32")


def _real_pauses(transcript: list[dict], min_gap_s: float, limit: int) -> list[float]:
    """The midpoint of each real gap between spoken words longer than
    `min_gap_s` — never an invented "chapter", only an actual silence the
    transcript itself shows."""
    if len(transcript) < 2:
        return []
    words = sorted(transcript, key=lambda w: w["start_s"])
    gaps = []
    for a, b in zip(words, words[1:]):
        gap = b["start_s"] - a["end_s"]
        if gap >= min_gap_s:
            gaps.append((gap, (a["end_s"] + b["start_s"]) / 2))
    gaps.sort(reverse=True)  # the longest, most natural pauses first
    return sorted(t for _, t in gaps[:limit])


def _insert_many(voice: np.ndarray, sr: int, inserts: list[tuple[float, np.ndarray]]) -> np.ndarray:
    """Splices each (time, clip) pair in at its own real position, latest
    first so an earlier insertion point never shifts because of one after it."""
    out = voice
    for t, clip in sorted(inserts, key=lambda pair: pair[0], reverse=True):
        at = min(len(out), int(t * sr))
        out = _join([out[:at], clip, out[at:]], sr)
    return out


def assemble_episode(
    *,
    voice_bytes: bytes,
    plan: AssemblePlan,
    intro_bytes: Optional[bytes] = None,
    outro_bytes: Optional[bytes] = None,
    sponsor_bytes: Optional[bytes] = None,
    bed_bytes: Optional[bytes] = None,
    transition_sfx_bytes: Optional[bytes] = None,
    signature_bytes: Optional[bytes] = None,
    transcript: Optional[list[dict]] = None,
) -> tuple[bytes, dict]:
    """Returns (wav_bytes, what_was_mixed)."""
    voice, sr = _decode(voice_bytes, "recording")
    channels = voice.shape[1]

    def prepared(data: bytes, what: str) -> np.ndarray:
        clip, clip_sr = _decode(data, what)
        return _match(clip, clip_sr, sr, channels)

    mixed: dict = {"voice_seconds": round(len(voice) / sr, 2)}

    if plan.voice_warmth:
        voice = np.stack([_warm(voice[:, c], plan.voice_warmth) for c in range(channels)], axis=1)
        mixed["voice_warmth"] = plan.voice_warmth

    # Every time-based insertion (the sponsor read, each transition sound) is
    # collected against the ORIGINAL voice's own timeline first, then spliced
    # in together in one pass — inserting one at a time would shift where
    # every later one lands.
    inserts: list[tuple[float, np.ndarray]] = []
    if plan.sponsor_at_s is not None:
        if not sponsor_bytes:
            raise AssembleError("There's no sponsor read set up yet.")
        sponsor = prepared(sponsor_bytes, "sponsor read")
        if plan.sponsor_at_s * sr > len(voice):
            raise AssembleError("The sponsor read is placed after the recording ends.")
        inserts.append((plan.sponsor_at_s, sponsor))
        mixed["sponsor_at_s"] = plan.sponsor_at_s
        mixed["sponsor_seconds"] = round(len(sponsor) / sr, 2)

    if plan.use_transition_sfx:
        if not transition_sfx_bytes:
            raise AssembleError("There's no transition sound set up yet.")
        pauses = _real_pauses(transcript or [], min_gap_s=1.2, limit=plan.max_transitions)
        if not pauses:
            raise AssembleError("This recording has no real pauses long enough for a transition sound.")
        transition = prepared(transition_sfx_bytes, "transition sound")
        # Sponsor's own moment never doubles as a transition point.
        pauses = [t for t in pauses if plan.sponsor_at_s is None or abs(t - plan.sponsor_at_s) > 1.0]
        inserts.extend((t, transition) for t in pauses)
        mixed["transitions_added"] = len(pauses)
        mixed["transition_times_s"] = pauses
        mixed["transition_seconds"] = round(len(transition) / sr, 2)

    body = _insert_many(voice, sr, inserts) if inserts else voice

    if plan.music_bed_id:
        if not bed_bytes:
            raise AssembleError("That music bed couldn't be found.")
        bed = prepared(bed_bytes, "music bed")
        body = _under_voice(body, bed, sr, plan)
        mixed["music_level_db"] = plan.music_level_db
        mixed["ducking_db"] = plan.ducking_db
        if plan.stereo_width and plan.stereo_width != 1.0 and channels == 2:
            mixed["stereo_width"] = plan.stereo_width

    pieces = []
    if plan.use_brand_signature:
        if not signature_bytes:
            raise AssembleError("There's no brand signature set up yet.")
        signature_clip = prepared(signature_bytes, "brand signature")
        pieces.append(signature_clip)
        mixed["brand_signature"] = True
        mixed["brand_signature_seconds"] = round(len(signature_clip) / sr, 2)
    if plan.use_intro:
        if not intro_bytes:
            raise AssembleError("There's no intro clip set up yet.")
        intro_clip = prepared(intro_bytes, "intro clip")
        pieces.append(intro_clip)
        mixed["intro"] = True
        mixed["intro_seconds"] = round(len(intro_clip) / sr, 2)
    pieces.append(body)
    if plan.use_outro:
        if not outro_bytes:
            raise AssembleError("There's no outro clip set up yet.")
        pieces.append(prepared(outro_bytes, "outro clip"))
        mixed["outro"] = True

    out = _join(pieces, sr)
    if len(out) / sr > MAX_SECONDS:
        raise AssembleError("The finished episode would be longer than 30 minutes.")
    peak = float(np.max(np.abs(out)))
    if peak > 0.99:
        out = out * (0.99 / peak)
    mixed["total_seconds"] = round(len(out) / sr, 2)

    buf = io.BytesIO()
    sf.write(buf, out if channels > 1 else out[:, 0], sr, format="WAV")
    return buf.getvalue(), mixed
