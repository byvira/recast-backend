"""Echo & room-noise reduction — real ElevenLabs Audio Isolation call.

A genuine external API call, not local DSP — audio_cleanup.py's own
docstring already says why: there is no dependable local method for echo
removal. Live-confirmed 2026-09-28: the ElevenLabs key configured for this
app returns a real `401 missing_permissions` on `/v1/audio-isolation` — the
account is on the free plan, which doesn't carry that permission. This
module is wired in ahead of that upgrade (the user's explicit choice) so
the feature starts working the moment the plan is upgraded and the key is
re-scoped, with no further code changes needed — it fails with a clear,
specific reason until then, never silently or with a generic error.
"""

import time
import httpx

from app.core.config import settings

AUDIO_ISOLATION_URL = "https://api.elevenlabs.io/v1/audio-isolation"


class EchoReductionError(Exception):
    """A message that is safe to show the member."""


from app.shared.llm_health.track import log_attempt, log_http  # noqa: E402

BASIC_CLEANUP_NOTE = (
    "Basic cleanup was used: it lowers steady background noise and low rumble. It cannot remove a true room echo, "
    "which needs the paid ElevenLabs plan."
)
# a gentle high-pass for rumble, then broadband noise reduction; loudness is left to the later steps
_BASIC_FILTER = "highpass=f=80,afftdn=nr=12:nf=-28:tn=1"


async def basic_cleanup(audio_bytes: bytes) -> bytes:
    """Steady noise and rumble reduction with ffmpeg, on our own server. It is honest about what it is: not echo removal.
    Raises EchoReductionError if ffmpeg cannot process the file."""
    import asyncio
    import tempfile
    from pathlib import Path

    import imageio_ffmpeg

    tmp = tempfile.mkdtemp(prefix="recast-cleanup-")
    try:
        src, dst = Path(tmp) / "in.audio", Path(tmp) / "out.wav"
        src.write_bytes(audio_bytes)
        proc = await asyncio.create_subprocess_exec(
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(src), "-af", _BASIC_FILTER, "-ar", "44100", str(dst),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            raise EchoReductionError("Basic cleanup took too long and was stopped. Try a shorter recording.")
        if proc.returncode != 0 or not dst.exists():
            raise EchoReductionError("Basic cleanup could not process this recording.")
        return dst.read_bytes()
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


async def reduce_echo(audio_bytes: bytes, filename: str = "audio.wav") -> bytes:
    """Sends the real audio bytes to ElevenLabs' Audio Isolation endpoint
    and returns the real cleaned bytes. Raises EchoReductionError with a
    specific, member-facing reason on any failure — never falls back to
    returning the original audio unchanged, which would silently hide that
    nothing actually happened."""
    if not settings.ELEVENLABS_API_KEY:
        raise EchoReductionError("Echo reduction needs an ElevenLabs API key, and none is configured yet.")

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                AUDIO_ISOLATION_URL,
                headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
                files={"audio": (filename, audio_bytes, "audio/wav")},
            )
    except httpx.HTTPError as exc:
        log_attempt("elevenlabs", "audio-isolation", t0, exc=exc, feature="echo_reduction")
        raise EchoReductionError("Could not reach the echo reduction service. Try again in a moment.") from exc
    log_http("elevenlabs", "audio-isolation", t0, response, feature="echo_reduction")

    if response.status_code == 401:
        detail = (response.json() or {}).get("detail", {})
        if isinstance(detail, dict) and detail.get("status") == "missing_permissions":
            raise EchoReductionError(
                "Echo reduction needs a paid ElevenLabs plan with the audio_isolation permission enabled "
                "on this workspace's API key. Upgrade the plan and re-scope the key to turn this on."
            )
        raise EchoReductionError("The echo reduction service rejected this request's credentials.")
    if response.status_code != 200:
        raise EchoReductionError("Echo reduction failed. Try again in a moment.")

    return response.content
