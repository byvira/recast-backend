"""A scheduled check of every model the product relies on: one tiny call each, kept in `llm_model_probe`, so a model that was retired, ran out
of quota or stopped answering is found by this check and not by a person whose request failed.

What the result feeds:
- the Fallback chains view on the Ops LLM health page (each step shows when it was last checked and whether it answered),
- the Ops bell (a retired or silent model),
- routing (`is_down`): the open-model backups skip a step known to be down instead of spending a wait on it. Every step down still means every
  step is tried, so a call never fails without trying.

The calls carry no member data (a one-word reply, one second of silence, a tiny picture, a fixed scene description) and go straight to the
providers, not through `track`, so a check never shows up as member traffic or opens a member-facing issue. Pictures and voice, which spend
real allowance, are checked once a day; everything else every run."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import struct
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.core.config import settings
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

PROMPT = "Reply with the single word: ok"
#: a full scene, because some picture services refuse a very short prompt as if it were unsafe
SCENE = "a calm wooden desk with a notebook and a cup of tea, soft morning light, photo"
PNG_8X8 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAF0lEQVR4nGP8z8Dwn4EIwESMolGFpCsEADbOAhHDLVkWAAAAAElFTkSuQmCC")

TIMEOUT_S = 45.0
RETRY_AFTER_S = 30.0          # a failed check is repeated once after this, so one blip is not a verdict
DAILY_AFTER = timedelta(hours=20)
DOWN_EXPIRES = timedelta(hours=9)   # a "down" reading older than this is not trusted for routing
CONCURRENCY = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def collection():
    return get_db()["llm_model_probe"]


async def create_indexes() -> None:
    try:
        await collection().create_index("key", unique=True)
    except Exception as exc:  # noqa: BLE001 - the check is optional: the app starts without it
        logger.warning("Could not create the model probe index: %s", exc)


@dataclass(frozen=True)
class Target:
    chain: str                      # one of the chain ids in chains.py
    provider: str
    model: str                      # the model id as the provider knows it
    call: Callable[[], Awaitable[str]]
    daily: bool = False             # spends real allowance (pictures, voice), so checked once a day
    route: str = ""                 # the key the open-model backups use for this step, so routing can skip it

    @property
    def key(self) -> str:
        return key_for(self.chain, self.provider, self.model)


def key_for(chain: str, provider: str, model: str) -> str:
    return f"{chain}:{provider}:{model}"


# ── the calls ─────────────────────────────────────────────────────────────────

def silent_wav(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * int(16000 * seconds))
    return buf.getvalue()


async def _chat(url: str, key: str, model: str, extra: Optional[dict] = None) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {key}"} if key else {},
                         json={"model": model, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 64, **(extra or {})})
        r.raise_for_status()
        r.json()["choices"][0]
    return "answered"


async def _openrouter_vision(model: str) -> str:
    uri = "data:image/png;base64," + base64.b64encode(PNG_8X8).decode()
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                         json={"model": model, "max_tokens": 32, "messages": [{"role": "user", "content": [{"type": "text", "text": "One word: the colour?"}, {"type": "image_url", "image_url": {"url": uri}}]}]})
        r.raise_for_status()
    return "answered"


async def _gemini(fn: Callable[[Any], Any]) -> str:
    from app.shared.llm import get_gemini_client

    client = get_gemini_client()
    await asyncio.get_running_loop().run_in_executor(None, lambda: fn(client))
    return "answered"


async def _gemini_text(model: str) -> str:
    return await _gemini(lambda c: c.models.generate_content(model=model, contents=PROMPT))


async def _gemini_vision(model: str) -> str:
    from google.genai import types

    return await _gemini(lambda c: c.models.generate_content(model=model, contents=[types.Part.from_bytes(data=PNG_8X8, mime_type="image/png"), "What colour is this? One word."]))


async def _gemini_embed() -> str:
    from app.shared.llm import EMBED_MODEL

    return await _gemini(lambda c: c.models.embed_content(model=EMBED_MODEL, contents="ok"))


async def _mistral_embed() -> str:
    from app.shared.embeddings import _mistral

    await _mistral(["ok"])
    return "answered"


async def _cloudflare_embed() -> str:
    from app.shared.embeddings import _cloudflare

    await _cloudflare(["ok"])
    return "answered"


async def _gemini_speech() -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S * 2) as c:
        r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_TTS_MODEL}:generateContent", headers={"x-goog-api-key": settings.GEMINI_API_KEY},
                         json={"contents": [{"parts": [{"text": "Say: ok"}]}], "generationConfig": {"responseModalities": ["AUDIO"],
                               "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": settings.GEMINI_TTS_VOICE}}}}})
        r.raise_for_status()
        r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    return "answered"


async def _groq_whisper(model: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                         data={"model": model}, files={"file": ("silence.wav", silent_wav(), "audio/wav")})
        r.raise_for_status()
    return "answered"


async def _deepgram_listen() -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post("https://api.deepgram.com/v1/listen?model=nova-3", headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}", "Content-Type": "audio/wav"}, content=silent_wav())
        r.raise_for_status()
    return "answered"


async def _deepgram_speak() -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post("https://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=mp3", headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"}, json={"text": "ok"})
        r.raise_for_status()
    return "answered"


async def _elevenlabs() -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post("https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM", headers={"xi-api-key": settings.ELEVENLABS_API_KEY}, json={"text": "ok", "model_id": "eleven_multilingual_v2"})
        r.raise_for_status()
    return "answered"


async def _voxtral() -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post("https://api.mistral.ai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {settings.MISTRAL_API_KEY}"},
                         data={"model": settings.VOXTRAL_MODEL}, files={"file": ("silence.wav", silent_wav(), "audio/wav")})
        r.raise_for_status()
    return "answered"


async def _cloudflare_picture(model: str) -> str:
    from app.shared.open_fallbacks import _cloudflare_backup

    if model.endswith("flux-1-schnell"):
        from app.pipelines.media.image_generation import _call_cloudflare_raw

        await _call_cloudflare_raw(SCENE)
    else:
        await _cloudflare_backup(model, SCENE)
    return "answered"


async def _pollinations_picture() -> str:
    from app.shared.open_fallbacks import _pollinations

    await _pollinations(SCENE)
    return "answered"


async def _stablehorde_picture() -> str:
    from app.shared.open_fallbacks import _stablehorde

    await _stablehorde(SCENE)
    return "answered"


def targets() -> list[Target]:
    """Every step of every chain that is switched on and has what it needs to run, built from the settings so it follows a changed model name."""
    from app.shared.llm import GeminiModel, GroqModel
    from app.shared import open_fallbacks as of

    out: list[Target] = []
    if settings.GROQ_API_KEY:
        for m in dict.fromkeys((GroqModel.BALANCED.value, GroqModel.FAST.value)):
            out.append(Target("writing", "groq", m, lambda m=m: _chat("https://api.groq.com/openai/v1/chat/completions", settings.GROQ_API_KEY, m, {"reasoning_effort": "low"})))
        for m in (GroqModel.WHISPER.value, GroqModel.WHISPER_TURBO.value):
            out.append(Target("speech_to_text", "groq", m, lambda m=m: _groq_whisper(m)))
    if settings.GEMINI_API_KEY:
        out.append(Target("writing", "gemini", GeminiModel.FLASH.value, lambda: _gemini_text(GeminiModel.FLASH.value)))
        out.append(Target("picture_understanding", "gemini", GeminiModel.FLASH.value, lambda: _gemini_vision(GeminiModel.FLASH.value)))
        from app.shared.llm import EMBED_MODEL

        out.append(Target("embeddings", "gemini", EMBED_MODEL, _gemini_embed))
        if settings.GEMINI_TTS_ENABLED:
            out.append(Target("voice", "gemini", settings.GEMINI_TTS_MODEL, _gemini_speech, daily=True))
    for p in of.text_providers():
        out.append(Target("writing", p.name, p.model, lambda p=p: _chat(p.url, p.key, p.model), route=f"text:{p.name}:{p.model}"))
    if settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID:
        out.append(Target("pictures", "cloudflare", "@cf/black-forest-labs/flux-1-schnell", lambda: _cloudflare_picture("@cf/black-forest-labs/flux-1-schnell"), daily=True))
        for m in [x.strip() for x in settings.CLOUDFLARE_IMAGE_BACKUP_MODELS.split(",") if x.strip()]:
            out.append(Target("pictures", "cloudflare", m, lambda m=m: _cloudflare_picture(m), daily=True, route=f"image:cloudflare {m.split('/')[-1]}"))
    if settings.POLLINATIONS_ENABLED:
        out.append(Target("pictures", "pollinations", "flux", _pollinations_picture, daily=True, route="image:pollinations"))
    if settings.STABLEHORDE_ENABLED:
        out.append(Target("pictures", "stablehorde", "anonymous", _stablehorde_picture, daily=True, route="image:stablehorde"))
    if settings.OPENROUTER_API_KEY:
        for m in dict.fromkeys(x for x in (settings.OPENROUTER_VISION_MODEL, settings.OPENROUTER_VISION_MODEL_2, settings.OPENROUTER_VISION_MODEL_3) if x):
            out.append(Target("picture_understanding", "openrouter", m, lambda m=m: _openrouter_vision(m), route=f"vision:openrouter:{m}"))
    if settings.DEEPGRAM_API_KEY:
        out.append(Target("voice", "deepgram", "aura-2", _deepgram_speak, daily=True))
        out.append(Target("speech_to_text", "deepgram", "nova-3", _deepgram_listen))
    if settings.ELEVENLABS_API_KEY and settings.ELEVENLABS_ENABLED:
        out.append(Target("voice", "elevenlabs", "eleven_multilingual_v2", _elevenlabs, daily=True))
    if settings.MISTRAL_API_KEY:
        out.append(Target("embeddings", "mistral", settings.MISTRAL_EMBED_MODEL, _mistral_embed, route=f"embeddings:mistral:{settings.MISTRAL_EMBED_MODEL}"))
    if settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID:
        out.append(Target("embeddings", "cloudflare", settings.CLOUDFLARE_EMBED_MODEL, _cloudflare_embed, daily=True, route=f"embeddings:cloudflare:{settings.CLOUDFLARE_EMBED_MODEL}"))
    if settings.MISTRAL_API_KEY and settings.VOXTRAL_MODEL:
        out.append(Target("speech_to_text", "mistral", settings.VOXTRAL_MODEL, _voxtral))
    return out


# ── reading an outcome ────────────────────────────────────────────────────────

RETIRED_WORDS = ("deprecated", "no longer supported", "no longer available", "decommissioned", "model not found", "not found for account", "does not exist", "model_not_found")


def read_failure(exc: BaseException) -> dict[str, Any]:
    """What a failed check says: state ok | limit | down | retired | unsure, the status, and a plain sentence (never the provider's own text)."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        body = (response.text or "")[:400].lower() if response is not None else ""
    except Exception:  # noqa: BLE001
        body = ""
    text = f"{body} {str(exc).lower()}"
    if status in (404, 410) or any(w in text for w in RETIRED_WORDS):
        return {"state": "retired", "status": status, "detail": "The provider no longer has this model. Change the model name."}
    if status in (401, 403):
        return {"state": "down", "status": status, "detail": "The provider refused the key."}
    if status == 402:
        return {"state": "down", "status": status, "detail": "The plan needs payment before this works."}
    if status == 429 or "quota" in text or "resource_exhausted" in text or "rate limit" in text:
        return {"state": "limit", "status": status, "detail": "The allowance is used up for now."}
    if status == 400 and any(w in text for w in ("nsfw", "unsafe", "content policy", "safety")):
        return {"state": "unsure", "status": status, "detail": "The provider's content filter refused the test prompt."}
    if status is not None and status >= 500:
        return {"state": "down", "status": status, "detail": "The provider returned a server error."}
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return {"state": "down", "status": None, "detail": "No answer in time."}
    return {"state": "down", "status": status, "detail": "The call failed."}


def settle(previous: Optional[dict], result: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """The new stored reading from the old one and this check's result. A check the provider could not judge ("unsure") changes nothing but the time."""
    prev = previous or {}
    streak = int(prev.get("fail_streak") or 0)
    state = result["state"]
    if state == "unsure":
        return {**prev, "checked_at": now}
    streak = 0 if state == "ok" else streak + 1
    return {**result, "fail_streak": streak, "checked_at": now, "last_ok_at": now if state == "ok" else prev.get("last_ok_at")}


# ── running the check ─────────────────────────────────────────────────────────

async def _one(t: Target) -> dict[str, Any]:
    async def attempt() -> dict[str, Any]:
        started = time.monotonic()
        try:
            await asyncio.wait_for(t.call(), timeout=TIMEOUT_S * 2)
            return {"state": "ok", "status": 200, "detail": "Answered.", "ms": int((time.monotonic() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            return {**read_failure(exc), "ms": int((time.monotonic() - started) * 1000)}

    first = await attempt()
    if first["state"] in ("ok", "unsure", "retired", "limit"):
        return first
    await asyncio.sleep(RETRY_AFTER_S)  # a blip should not be a verdict
    return await attempt()


async def run(*, force: bool = False) -> dict[str, Any]:
    """Checks every target that is due (all of them when `force`) and stores the readings. Returns a summary. Never raises."""
    summary: dict[str, Any] = {"checked": 0, "ok": 0, "problems": []}
    try:
        all_targets = targets()
        stored = {d["key"]: d async for d in collection().find({"key": {"$in": [t.key for t in all_targets]}})}
        now = _now()
        due = [t for t in all_targets if force or not t.daily or not stored.get(t.key) or now - _aware(stored[t.key].get("checked_at")) >= DAILY_AFTER]
        gate = asyncio.Semaphore(CONCURRENCY)

        async def guarded(t: Target) -> tuple[Target, dict[str, Any]]:
            async with gate:
                return t, await _one(t)

        for t, result in await asyncio.gather(*(guarded(t) for t in due)):
            doc = settle(stored.get(t.key), result, now=_now())
            doc.update({"key": t.key, "chain": t.chain, "provider": t.provider, "model": t.model, "route": t.route, "ms": result.get("ms")})
            doc.pop("_id", None)
            await collection().update_one({"key": t.key}, {"$set": doc}, upsert=True)
            summary["checked"] += 1
            if doc["state"] == "ok":
                summary["ok"] += 1
            else:
                summary["problems"].append({"key": t.key, "state": doc["state"], "detail": doc["detail"]})
        # a model that is no longer in any chain stops being reported
        await collection().delete_many({"key": {"$nin": [t.key for t in all_targets]}})
        await refresh(force=True)
    except Exception as exc:  # noqa: BLE001 - a failed check must never affect anything else
        logger.warning("The model check failed: %s", exc)
        summary["error"] = "The check could not finish."
    return summary


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


# ── what the rest of the product reads ───────────────────────────────────────

_down: dict[str, str] = {}
_loaded_at = 0.0
CACHE_SECONDS = 120.0


async def refresh(*, force: bool = False) -> None:
    """Keeps a small in-memory copy of which routes are known to be down, read from the database at most every two minutes, so every server sees what
    the check found. Failing to read it leaves the last copy in place."""
    global _loaded_at, _down
    if not force and time.monotonic() - _loaded_at < CACHE_SECONDS:
        return
    _loaded_at = time.monotonic()
    try:
        cutoff = _now() - DOWN_EXPIRES
        docs = await collection().find({"route": {"$ne": ""}, "state": {"$in": ["down", "retired", "limit"]}, "checked_at": {"$gte": cutoff}}, {"route": 1, "state": 1}).to_list(200)
        _down = {d["route"]: d["state"] for d in docs}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read the model check: %s", exc)


def is_down(route: str) -> bool:
    """True when the last check found this open-model step down, retired or out of allowance (and that reading is recent)."""
    return route in _down


async def all_readings() -> dict[str, dict[str, Any]]:
    """Every stored reading by key, for the health page."""
    out: dict[str, dict[str, Any]] = {}
    async for d in collection().find({}, {"_id": 0}):
        out[d["key"]] = d
    return out


def step_state(reading: Optional[dict], *, now: Optional[datetime] = None) -> Optional[str]:
    """The chain step status a reading implies, or None when there is nothing recent to say: healthy, limit_reached or down."""
    if not reading:
        return None
    now = now or _now()
    state = reading.get("state")
    if state == "ok":
        return "healthy"
    if state in ("retired", "limit", "down") and now - _aware(reading.get("checked_at")) <= DOWN_EXPIRES:
        return "limit_reached" if state == "limit" else "down"
    return None
