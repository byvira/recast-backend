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

import httpx

from app.core.config import settings

AUDIO_ISOLATION_URL = "https://api.elevenlabs.io/v1/audio-isolation"


class EchoReductionError(Exception):
    """A message that is safe to show the member."""


async def reduce_echo(audio_bytes: bytes, filename: str = "audio.wav") -> bytes:
    """Sends the real audio bytes to ElevenLabs' Audio Isolation endpoint
    and returns the real cleaned bytes. Raises EchoReductionError with a
    specific, member-facing reason on any failure — never falls back to
    returning the original audio unchanged, which would silently hide that
    nothing actually happened."""
    if not settings.ELEVENLABS_API_KEY:
        raise EchoReductionError("Echo reduction needs an ElevenLabs API key, and none is configured yet.")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                AUDIO_ISOLATION_URL,
                headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
                files={"audio": (filename, audio_bytes, "audio/wav")},
            )
    except httpx.HTTPError as exc:
        raise EchoReductionError("Could not reach the echo reduction service. Try again in a moment.") from exc

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
