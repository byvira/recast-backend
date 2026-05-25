"""
llm.py — Complete end-to-end LLM utility.

Providers
  Primary  : Groq  — text generation, structured JSON, transcription
  Vision   : Gemini — image analysis, keyframe scoring
  Fallback : Gemini — plain text when Groq rate limits are severe

Public surface
  call_llm()                       plain text (Groq)
  call_llm_stream()                streaming plain text (Groq)
  call_llm_structured()            structured JSON (Groq)
  call_vision()                    image analysis (Gemini)
  call_llm_fallback()              emergency plain text (Gemini)
  call_llm_structured_fallback()   emergency JSON (Gemini)
  transcribe_audio()               Whisper transcription (Groq)
  llm_health_check()               ping both providers
  get_usage_stats()                token/call counters for the current process

All JSON parsing is delegated to app.utils.jsonparser.parse_llm_json.
No inline parsing logic exists in this file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from enum import Enum
from typing import Any, AsyncIterator

import aiofiles
from fastapi import HTTPException
from google import genai
from google.genai import types
from groq import APIConnectionError, AsyncGroq, RateLimitError

from app.core.config import settings
from app.utils.jsonparser import parse_llm_json

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────

class GroqModel(str, Enum):
    FAST      = "llama-3.1-8b-instant"          # simple / single-field JSON
    BALANCED  = "llama-3.3-70b-versatile"       # generation, hooks, SEO, repurpose
    POWERFUL  = "llama-3.1-70b-versatile"       # complex multi-step reasoning
    REASONING = "deepseek-r1-distill-llama-70b" # deep reasoning
    WHISPER   = "whisper-large-v3"              


class GeminiModel(str, Enum):
    FLASH      = "gemini-2.5-flash"
    FLASH_LITE = "gemini-2.5-flash-lite"
    PRO        = "gemini-2.5-pro"


# ─────────────────────────────────────────────────────────────
# Token / call usage tracking (in-process counters)
# ─────────────────────────────────────────────────────────────

_usage: dict[str, int] = defaultdict(int)
# keys: "{model_name}.prompt_tokens"
#       "{model_name}.completion_tokens"
#       "{model_name}.calls"


def _record_usage(model_name: str, usage: Any) -> None:
    """Accumulate token counts from a Groq completion usage object."""
    if usage is None:
        return
    _usage[f"{model_name}.calls"]             += 1
    _usage[f"{model_name}.prompt_tokens"]     += getattr(usage, "prompt_tokens", 0)
    _usage[f"{model_name}.completion_tokens"] += getattr(usage, "completion_tokens", 0)


def get_usage_stats() -> dict[str, int]:
    """Return a snapshot of accumulated token / call counters."""
    return dict(_usage)


# ─────────────────────────────────────────────────────────────
# Clients — singletons
# ─────────────────────────────────────────────────────────────

_groq_client: AsyncGroq | None = None
_gemini_client: genai.Client | None = None


def get_groq_client() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        _groq_client = AsyncGroq(
            api_key=settings.GROQ_API_KEY,
            timeout=30.0,
            max_retries=2,
        )
    return _groq_client


def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _gemini_client


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

_JSON_SYSTEM_SUFFIX = (
    "Respond ONLY with valid JSON. "
    "No explanation, no markdown, no backticks. "
    "Start with { or ["
)

# Approximate chars-per-token for prompt budget estimation (conservative)
_CHARS_PER_TOKEN = 3.5


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _safe_max_tokens(prompt: str, system: str, ceiling: int) -> int:
    """
    Reduce max_tokens if the prompt itself is large so we don't
    exceed the model context window. Reserves the ceiling for output.
    Most Groq models are 8k-128k; we guard against the smallest (8k).
    """
    used = _estimate_tokens(prompt) + _estimate_tokens(system)
    return max(512, min(ceiling, 8_000 - used))


def _build_messages(
    prompt: str,
    system: str = "",
    json_mode: bool = False,
) -> list[dict[str, str]]:
    """Assemble the messages list for a Groq chat completion."""
    parts: list[str] = []
    if system:
        parts.append(system)
    if json_mode:
        parts.append(_JSON_SYSTEM_SUFFIX)

    messages: list[dict[str, str]] = []
    if parts:
        messages.append({"role": "system", "content": "\n\n".join(parts)})
    messages.append({"role": "user", "content": prompt})
    return messages


def _log_if_truncated(response: Any, context: str) -> None:
    """Warn when the model stopped before a natural end."""
    reason = response.choices[0].finish_reason
    if reason != "stop":
        logger.warning(
            "%s — response truncated: finish_reason=%s usage=%s",
            context, reason, response.usage,
        )


def _raw_text(response: Any) -> str:
    return response.choices[0].message.content or ""


# ─────────────────────────────────────────────────────────────
# Exponential backoff for rate limits
# ─────────────────────────────────────────────────────────────

async def _backoff_retry(
    coro_factory,
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    label: str = "llm",
) -> Any:
    """
    Retry coro_factory() up to `attempts` times on RateLimitError,
    using exponential back-off: 2s, 4s, 8s, ...

    Raises the last RateLimitError if all attempts are exhausted.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except RateLimitError as exc:
            if attempt == attempts:
                raise
            delay = base_delay ** attempt
            logger.warning(
                "%s rate limit (attempt %d/%d) — retrying in %.0f s",
                label, attempt, attempts, delay,
            )
            await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────────────
# 1. Plain text generation — Groq
# ─────────────────────────────────────────────────────────────

async def call_llm(
    prompt: str,
    model: GroqModel = GroqModel.BALANCED,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 2500,
) -> str:
    """
    Plain text generation via Groq.

    Retries with exponential back-off on RateLimitError (up to 3 attempts),
    then falls back to FAST model, then raises HTTP 503.
    """
    client   = get_groq_client()
    messages = _build_messages(prompt, system)
    capped   = _safe_max_tokens(prompt, system, max_tokens)

    async def _complete(mdl: GroqModel) -> str:
        resp = await client.chat.completions.create(
            model=mdl.value,
            messages=messages,
            temperature=temperature,
            max_tokens=capped,
        )
        _log_if_truncated(resp, f"call_llm({mdl.name})")
        _record_usage(mdl.value, resp.usage)
        return _raw_text(resp)

    try:
        return await _backoff_retry(
            lambda: _complete(model),
            label=f"call_llm({model.name})",
        )

    except RateLimitError:
        logger.warning("Groq rate limit persists — falling back to FAST model")
        try:
            return await _complete(GroqModel.FAST)
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="LLM rate limit. Please try again in 60 seconds.",
            )

    except APIConnectionError as exc:
        logger.error("Groq connection error: %s", exc)
        raise HTTPException(status_code=503, detail="LLM service unavailable.")

    except Exception as exc:
        logger.error("Unexpected call_llm error: %s", exc)
        raise HTTPException(status_code=500, detail="Unexpected error during text generation.")


# ─────────────────────────────────────────────────────────────
# 2. Streaming plain text — Groq
# ─────────────────────────────────────────────────────────────

async def call_llm_stream(
    prompt: str,
    model: GroqModel = GroqModel.BALANCED,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 2500,
) -> AsyncIterator[str]:
    """
    Streaming plain text generation via Groq.

    Yields text chunks as they arrive. Use for SSE / WebSocket endpoints.

    Usage:
        async for chunk in call_llm_stream(prompt):
            await websocket.send_text(chunk)
    """
    client   = get_groq_client()
    messages = _build_messages(prompt, system)
    capped   = _safe_max_tokens(prompt, system, max_tokens)

    try:
        stream = await client.chat.completions.create(
            model=model.value,
            messages=messages,
            temperature=temperature,
            max_tokens=capped,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    except RateLimitError:
        raise HTTPException(
            status_code=503,
            detail="LLM rate limit. Please try again in 60 seconds.",
        )
    except APIConnectionError as exc:
        logger.error("Groq stream connection error: %s", exc)
        raise HTTPException(status_code=503, detail="LLM service unavailable.")
    except Exception as exc:
        logger.error("Unexpected call_llm_stream error: %s", exc)
        raise HTTPException(status_code=500, detail="Unexpected streaming error.")


# ─────────────────────────────────────────────────────────────
# 3. Structured JSON output — Groq
# ─────────────────────────────────────────────────────────────

async def call_llm_structured(
    prompt: str,
    system: str = "",
    model: GroqModel = GroqModel.BALANCED,
    max_tokens: int = 2500,
) -> dict[str, Any]:
    """
    Structured JSON output via Groq.

    All parsing is delegated to parse_llm_json which handles:
      - Extra data / concatenated objects
      - Markdown fences
      - Control characters in strings
      - Truncated output

    Returns {} on any parse failure — never raises.

    Model guidance:
      BALANCED (default) — complex, multi-layer prompts
      FAST               — simple single-field extraction
    """
    client   = get_groq_client()
    messages = _build_messages(prompt, system, json_mode=True)
    capped   = _safe_max_tokens(prompt, system, max_tokens)

    async def _complete_and_parse(mdl: GroqModel) -> dict[str, Any]:
        resp = await client.chat.completions.create(
            model=mdl.value,
            messages=messages,
            temperature=0.3,
            max_tokens=capped,
        )
        _log_if_truncated(resp, f"call_llm_structured({mdl.name})")
        _record_usage(mdl.value, resp.usage)
        return parse_llm_json(_raw_text(resp))

    try:
        return await _backoff_retry(
            lambda: _complete_and_parse(model),
            label=f"call_llm_structured({model.name})",
        )

    except RateLimitError:
        logger.error("Groq structured call rate limit persisted after retries")
        return {}

    except APIConnectionError as exc:
        logger.error("Groq connection error: %s", exc)
        return {}

    except Exception as exc:
        logger.error("Unexpected call_llm_structured error: %s", exc)
        return {}


# ─────────────────────────────────────────────────────────────
# 4. Vision / image analysis — Gemini only
# ─────────────────────────────────────────────────────────────

async def call_vision(
    prompt: str,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    model: GeminiModel = GeminiModel.FLASH,
) -> str:
    """
    Image analysis via Gemini Vision.
    Groq does not support vision — Gemini is the only option here.
    Used by: image pipeline, video thumbnail scoring, keyframe analysis.
    """
    client = get_gemini_client()
    loop   = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=model.value,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt,
                ],
            ),
        )
        return response.text or ""

    except Exception as exc:
        logger.error("Gemini vision call failed: %s", exc)
        return ""


# ─────────────────────────────────────────────────────────────
# 5. Transcription — Groq Whisper
# ─────────────────────────────────────────────────────────────

async def transcribe_audio(
    file_path: str,
    language: str = "en",
) -> dict[str, Any]:
    """
    Transcribe audio/video with timestamps via Groq Whisper.

    Returns:
        {
            "text":       str,
            "segments":   [{"start": float, "end": float, "text": str}],
            "duration_s": float,  # total duration (last segment end)
        }
    """
    client = get_groq_client()

    async with aiofiles.open(file_path, "rb") as f:
        audio_bytes = await f.read()

    response = await client.audio.transcriptions.create(
        model=GroqModel.WHISPER.value,
        file=(file_path, audio_bytes),
        language=language,
        response_format="verbose_json",
        timestamp_granularities=["segment"],
    )

    segments = [
        {"start": seg.start, "end": seg.end, "text": seg.text}
        for seg in (response.segments or [])
    ]

    return {
        "text":       response.text,
        "segments":   segments,
        "duration_s": segments[-1]["end"] if segments else 0.0,
    }


# ─────────────────────────────────────────────────────────────
# 6. Gemini plain-text fallback (severe Groq rate limits only)
# ─────────────────────────────────────────────────────────────

async def call_llm_fallback(
    prompt: str,
    system: str = "",
    model: GeminiModel = GeminiModel.FLASH,
) -> str:
    """
    Emergency plain-text generation via Gemini.
    Not used in normal flow — only when Groq is fully unavailable.
    """
    client      = get_gemini_client()
    loop        = asyncio.get_running_loop()
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=model.value,
                contents=full_prompt,
            ),
        )
        return response.text or ""

    except Exception as exc:
        logger.error("Gemini fallback failed: %s", exc)
        raise HTTPException(status_code=503, detail="LLM service unavailable.")


# ─────────────────────────────────────────────────────────────
# 7. Gemini structured JSON fallback
# ─────────────────────────────────────────────────────────────

async def call_llm_structured_fallback(
    prompt: str,
    system: str = "",
    model: GeminiModel = GeminiModel.FLASH,
) -> dict[str, Any]:
    """
    Emergency structured JSON via Gemini.
    Mirrors call_llm_structured but uses Gemini when Groq is fully down.
    Returns {} on failure — never raises.
    """
    system_with_json = f"{system}\n\n{_JSON_SYSTEM_SUFFIX}" if system else _JSON_SYSTEM_SUFFIX
    raw = await call_llm_fallback(prompt=prompt, system=system_with_json, model=model)
    return parse_llm_json(raw)


# ─────────────────────────────────────────────────────────────
# 8. Health check
# ─────────────────────────────────────────────────────────────

async def llm_health_check() -> dict[str, Any]:
    """
    Ping both providers with a minimal prompt.
    Returns a status dict suitable for a /health endpoint.

    Example response:
        {
            "groq":   {"status": "ok",    "latency_ms": 312},
            "gemini": {"status": "error", "detail": "..."},
        }
    """
    result: dict[str, Any] = {}

    # --- Groq ----------------------------------------------------------
    t0 = time.monotonic()
    try:
        client = get_groq_client()
        resp   = await client.chat.completions.create(
            model=GroqModel.FAST.value,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0.0,
        )
        _ = _raw_text(resp)
        result["groq"] = {
            "status":     "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:
        result["groq"] = {"status": "error", "detail": str(exc)}

    # --- Gemini --------------------------------------------------------
    t0 = time.monotonic()
    try:
        client = get_gemini_client()
        loop   = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=GeminiModel.FLASH_LITE.value,
                contents="ping",
            ),
        )
        result["gemini"] = {
            "status":     "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:
        result["gemini"] = {"status": "error", "detail": str(exc)}

    return result