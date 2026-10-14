"""Which voice speaks a line, and how, for dubs, narration and flows.

Order of preference, always: the premium voice when the member has one set up (ElevenLabs, which the shared speech code tries first),
then the open-source natural voice on the worker (Kokoro) for the languages it reads, then the draft voice (Deepgram). A line that the
natural voice cannot make (worker offline, language not read, a failed job) falls back to the draft voice, so a dub is never blocked by
the worker, and the result says which voice was used.

The natural voice is made for a whole set of lines in one worker job, not one job per line: call `prepare` with every line first.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.core.config import settings
from app.db.mongo import get_db
from app.video import jobs

logger = logging.getLogger(__name__)

WAIT_SECONDS = 300
POLL_SECONDS = 2.0
CHUNK = 60
NATURAL_LANGUAGES = {"en", "es", "fr", "hi", "it", "ja", "pt", "zh"}
KOKORO_VOICES = {"af_heart", "af_bella", "am_michael", "bf_emma", "ef_dora", "em_alex", "ff_siwis", "hf_alpha", "hm_omega", "if_sara", "im_nicola",
                 "jf_alpha", "jm_kumo", "pf_dora", "pm_alex", "zf_xiaobei"}

Fallback = Callable[[str, Optional[str]], Awaitable[Optional[bytes]]]
Fetch = Callable[[str], Awaitable[Optional[bytes]]]
Forget = Callable[[str], Awaitable[None]]


def premium_ready(voice_settings: Any) -> bool:
    """True when ElevenLabs is switched on and the member picked a real voice, so the shared speech code will use it first."""
    voice_id = (getattr(voice_settings, "tts_voice", "") or "").strip().lower()
    return bool(settings.ELEVENLABS_API_KEY and settings.ELEVENLABS_ENABLED and voice_id and voice_id not in ("piper_lessac",)
                and not voice_id.startswith("aura-"))


async def worker_languages() -> list[str]:
    """The languages the connected worker can speak with the natural voice, empty when it is offline or has no voice."""
    if not await jobs.worker_ready():
        return []
    state = await jobs.worker_status()
    caps = state.get("capabilities") or {}
    if not caps.get("speech") or not state.get("online"):
        return []
    return [str(c).split("-")[0] for c in caps.get("speech_languages") or []]


async def _download(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            res = await client.get(url)
        return res.content if res.status_code == 200 and 0 < len(res.content) <= 25 * 1024 * 1024 else None
    except httpx.HTTPError as exc:
        logger.warning("Could not read a spoken line: %s", exc)
        return None


class Voice:
    """A speak(text, speaker) callable with `prepare` for batching, and a record of which voice made what in `used`."""

    def __init__(
        self, *, workspace_id: str, user_id: str, language: Optional[str], fallback: Fallback, voice_settings: Any = None,
        voices: Optional[dict[str, str]] = None, fetch: Fetch = _download, forget: Optional[Forget] = None, languages: Optional[list[str]] = None,
        wait_seconds: float = WAIT_SECONDS,
    ):
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.language = (language or "en").lower().split("-")[0]
        self.fallback = fallback
        self.voice_settings = voice_settings
        self.voices = voices or {}
        self.fetch = fetch
        self.forget = forget
        self._languages = languages
        self.wait_seconds = wait_seconds
        self._cache: dict[tuple[str, Optional[str]], bytes] = {}
        self.used: dict[str, int] = {"natural": 0, "premium": 0, "draft": 0}
        self.notes: list[str] = []

    def for_language(self, language: Optional[str]) -> "Voice":
        """The same voice settings for another language (a flow learns its language after the voice was made)."""
        other = Voice(workspace_id=self.workspace_id, user_id=self.user_id, language=language, fallback=self.fallback, voice_settings=self.voice_settings,
                      voices=self.voices, fetch=self.fetch, forget=self.forget, languages=self._languages, wait_seconds=self.wait_seconds)
        other.used, other.notes = self.used, self.notes
        return other

    def _voice_for(self, speaker: Optional[str]) -> Optional[str]:
        v = self.voices.get(speaker or "")
        return v if v in KOKORO_VOICES else None

    async def natural_available(self) -> bool:
        if premium_ready(self.voice_settings) or self.language not in NATURAL_LANGUAGES:
            return False
        langs = self._languages if self._languages is not None else await worker_languages()
        return self.language in langs

    async def prepare(self, items: list[Any]) -> None:
        """Make all the lines with the natural voice in as few worker jobs as possible. `items` are texts or (text, speaker) pairs.
        Anything that cannot be made stays out of the cache and is spoken by the draft voice when asked for."""
        pairs = [(i, None) if isinstance(i, str) else (i[0], i[1] if len(i) > 1 else None) for i in items]
        pairs = [p for p in dict.fromkeys(pairs) if p[0].strip()]
        if not pairs or not await self.natural_available():
            return
        by_voice: dict[Optional[str], list[tuple[str, Optional[str]]]] = {}
        for text, speaker in pairs:
            by_voice.setdefault(self._voice_for(speaker), []).append((text, speaker))
        for voice, group in by_voice.items():
            for k in range(0, len(group), CHUNK):
                await self._run_chunk(group[k:k + CHUNK], voice)

    async def _run_chunk(self, chunk: list[tuple[str, Optional[str]]], voice: Optional[str]) -> None:
        lines = [{"id": f"l{n}", "text": text} for n, (text, _) in enumerate(chunk)]
        try:
            job = await jobs.enqueue_speech(workspace_id=self.workspace_id, user_id=self.user_id, items=lines, language=self.language, voice=voice)
        except jobs.JobError as exc:
            self.notes.append(exc.message)
            return
        began = time.monotonic()
        while True:
            current = await jobs.get_job(self.workspace_id, job["id"])
            if current and current["status"] == "done":
                break
            if not current or current["status"] in ("failed", "cancelled", "blocked"):
                self.notes.append("The natural voice could not make some lines, so the draft voice was used for them.")
                return
            if time.monotonic() - began > self.wait_seconds:
                await jobs.cancel_job(self.workspace_id, job["id"])
                self.notes.append("The natural voice took too long, so the draft voice was used.")
                return
            await asyncio.sleep(POLL_SECONDS)
        row = await get_db()["video_speech"].find_one({"job_id": job["id"], "workspace_id": self.workspace_id})
        by_id = {it["id"]: it for it in (row or {}).get("items", [])}
        for n, (text, speaker) in enumerate(chunk):
            item = by_id.get(f"l{n}")
            if not item:
                continue
            data = await self.fetch(item["url"])
            if data:
                self._cache[(text, speaker)] = data
            if self.forget:
                try:
                    await self.forget(item["url"])
                except Exception as exc:  # noqa: BLE001 - a leftover temporary file never fails the dub
                    logger.warning("Could not remove a temporary voice file: %s", exc)

    async def __call__(self, text: str, speaker: Optional[str] = None) -> Optional[bytes]:
        hit = self._cache.get((text, speaker))
        if hit is not None:
            self.used["natural"] += 1
            return hit
        audio = await self.fallback(text, speaker)
        if audio:
            self.used["premium" if premium_ready(self.voice_settings) else "draft"] += 1
        return audio

    def summary(self) -> dict[str, Any]:
        """Which voice made the lines: "natural", "premium", "draft" or "mixed", and any notes for the member."""
        kinds = [k for k, n in self.used.items() if n]
        return {"voice": kinds[0] if len(kinds) == 1 else ("mixed" if kinds else None), "counts": dict(self.used), "notes": list(self.notes)}
