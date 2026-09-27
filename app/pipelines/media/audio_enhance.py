"""Real DSP cleanup — closes the "DSP cleanup wiring" gap named in
PROGRESS.md's Deferred list. Core real ops per file 02/06's own scope
decision (see PROGRESS.md's Decisions Log): `noisereduce` (denoise) +
`pyloudnorm` (loudness, real targets -16 LUFS stereo / -19 LUFS mono,
standard podcast delivery spec). More exotic toggles already in the mock
UI (de-esser, harmonic exciter) deliberately stay unbuilt per that same
decision — "don't build fake depth to match every slider."

No ffmpeg/pydub dependency, matching transform.py's own "no ffmpeg, no
new infra" precedent as closely as this feature allows — decode/encode
via `soundfile` (bundled libsndfile 1.2+, real MP3/WAV/OGG support, no
system package required, confirmed by direct test 2026-09-26). M4A/WEBM
have no soundfile decode path and are honestly reported as unsupported
via `is_dsp_supported` rather than silently skipped or guessed at.
"""

import io
import logging

import noisereduce as nr
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.signal import resample

logger = logging.getLogger(__name__)

# soundfile's bundled libsndfile (1.2+) decodes these for real, confirmed
# directly (sf.available_formats()) — M4A/WEBM are real accepted upload
# types elsewhere (media.py's ALLOWED_MIME_TYPES) but have no decode path
# here, so DSP enhancement is honestly skipped for them, not guessed at.
_SUPPORTED_DECODE_TYPES = {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3", "audio/ogg"}


def is_dsp_supported(mime_type: str) -> bool:
    return (mime_type or "").lower() in _SUPPORTED_DECODE_TYPES


def enhance_audio(audio_bytes: bytes, mime_type: str) -> tuple[bytes, dict]:
    """Real denoise + loudness-normalize. Returns (enhanced_wav_bytes,
    dsp_settings_applied). Raises on decode failure — the caller should
    check `is_dsp_supported` first and skip calling this for an
    unsupported format rather than treating an exception as the normal
    "not supported" path."""
    data, samplerate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    is_mono = data.ndim == 1

    if is_mono:
        denoised = nr.reduce_noise(y=data, sr=samplerate)
    else:
        # noisereduce operates per-channel; stack the per-channel results
        # back into the original (samples, channels) shape.
        denoised = np.stack(
            [nr.reduce_noise(y=data[:, ch], sr=samplerate) for ch in range(data.shape[1])],
            axis=1,
        )

    meter = pyln.Meter(samplerate)
    measured_loudness = meter.integrated_loudness(denoised)
    # Real podcast delivery targets (pow/audio_image_pipeline/06-full-
    # workflow-and-localization.md step 5): -16 LUFS stereo, -19 LUFS mono.
    target_lufs = -19.0 if is_mono else -16.0
    normalized = pyln.normalize.loudness(denoised, measured_loudness, target_lufs)

    out_buf = io.BytesIO()
    sf.write(out_buf, normalized, samplerate, format="WAV")
    settings = {
        "denoise": True,
        "loudness_normalize": True,
        "target_lufs": target_lufs,
        "measured_loudness_before_lufs": measured_loudness,
    }
    return out_buf.getvalue(), settings


def concatenate_turns(turn_audio_bytes: list[bytes]) -> bytes:
    """Real multi-voice dialogue stitching (closes the "multi-voice
    dialogue" gap named in PROGRESS.md's Deferred list) — decodes each
    turn's real TTS output (soundfile handles both providers' real MP3
    output, confirmed earlier this sweep), resamples any turn that came
    back at a different sample rate than the first (a real possibility
    if ElevenLabs served some turns and the Deepgram fallback served
    others mid-dialogue) to the first turn's rate, converts every turn
    to mono (dialogue turns don't need stereo), and concatenates them
    into one continuous real WAV file — no silence-fitting/crossfade,
    turns simply play back to back in order."""
    if not turn_audio_bytes:
        raise ValueError("concatenate_turns needs at least one turn.")

    decoded: list[np.ndarray] = []
    target_sr: int | None = None
    for turn_bytes in turn_audio_bytes:
        data, sr = sf.read(io.BytesIO(turn_bytes), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        if target_sr is None:
            target_sr = sr
        elif sr != target_sr:
            data = resample(data, int(len(data) * target_sr / sr))
        decoded.append(data)

    full = np.concatenate(decoded)
    out_buf = io.BytesIO()
    sf.write(out_buf, full, target_sr, format="WAV")
    return out_buf.getvalue()
