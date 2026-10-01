"""Records calls to AI providers that do not go through the chat functions in app.shared.llm: speech to text
(Groq Whisper), text to speech (ElevenLabs, Deepgram), picture generation (Cloudflare Workers AI, Gemini image)
and echo reduction (ElevenLabs). The chat functions use `log_attempt` directly; these use `track`.

Recording never raises and never changes what the call does: `track` does not swallow an error, it only notes it.
Nothing but a scrubbed provider message is stored."""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.shared.llm_health.scrub import scrub_message

_fallback_on: contextvars.ContextVar[bool] = contextvars.ContextVar("llm_health_fallback_on", default=False)


@contextmanager
def fallback_scope() -> Iterator[None]:
    """Attempts made inside this block are the fallback that covers for a provider that failed."""
    token = _fallback_on.set(True)
    try:
        yield
    finally:
        _fallback_on.reset(token)


def usage_numbers(usage: Any) -> tuple[int, int, int]:
    """(prompt, completion, cached) tokens from a Groq or Gemini usage object; zeros when absent."""
    if usage is None:
        return 0, 0, 0
    pt = getattr(usage, "prompt_tokens", None)
    if pt is None:
        return (int(getattr(usage, "prompt_token_count", 0) or 0), int(getattr(usage, "candidates_token_count", 0) or 0),
                int(getattr(usage, "cached_content_token_count", 0) or 0))
    return (int(pt or 0), int(getattr(usage, "completion_tokens", 0) or 0),
            int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0))


def _status_of(exc: BaseException) -> int | None:
    for holder in (exc, getattr(exc, "response", None)):
        for name in ("status_code", "code"):
            value = getattr(holder, name, None)
            if isinstance(value, int):
                return value
    return None


def _message_of(exc: BaseException) -> str:
    text = str(exc)
    body = getattr(getattr(exc, "response", None), "text", None)
    if isinstance(body, str) and body and body[:80] not in text:
        text = f"{text} {body[:300]}"  # providers put the reason ("quota_exceeded") in the body
    return scrub_message(text, 500)


def refused_outright(exc: BaseException) -> bool:
    """True when a provider said no for a reason that retrying will not fix in the next minutes: a rejected key,
    missing access or plan, or a used-up allowance. Callers use it to skip that provider for a while."""
    try:
        from app.shared.llm_health.classifier import classify

        kind = classify(http_status=_status_of(exc), error_class=exc.__class__.__name__, message=_message_of(exc))
        return kind in ("auth_invalid_key", "billing_or_access", "model_unavailable", "quota_exhausted")
    except Exception:  # noqa: BLE001
        return False


def log_attempt(
    provider: str, model: str, t0: float, *, result: Any = None, exc: BaseException | None = None,
    feature: str | None = None, fallback: bool | None = None,
) -> None:
    """Hands one attempt to the durable recorder. Never raises and never waits."""
    try:
        from app.shared.llm_health.recorder import Attempt, recorder

        elapsed = (time.perf_counter() - t0) * 1000
        fell_back = _fallback_on.get() if fallback is None else fallback
        if exc is None:
            usage = getattr(result, "usage", None) or getattr(result, "usage_metadata", None)
            tin, tout, cached = usage_numbers(usage)
            recorder.record(Attempt(
                provider=provider, model=model, ok=True, latency_ms=elapsed, tokens_in=tin, tokens_out=tout, cached_tokens=cached,
                feature=feature, outcome="fallback_success" if fell_back else None, fallback_to=f"{provider}:{model}" if fell_back else None,
            ))
            return
        raw = getattr(getattr(exc, "response", None), "headers", None)
        headers, retry_after = None, None
        if raw is not None:
            try:
                headers = {k: str(v) for k, v in raw.items() if str(k).lower().startswith(("x-ratelimit", "retry-after"))} or None
                if raw.get("retry-after") is not None:
                    retry_after = float(raw.get("retry-after"))
            except Exception:  # noqa: BLE001
                headers = None
        recorder.record(Attempt(
            provider=provider, model=model, ok=False, latency_ms=elapsed, http_status=_status_of(exc),
            error_class=exc.__class__.__name__, error_message=_message_of(exc), retry_after_s=retry_after, rate_headers=headers, feature=feature,
        ))
    except Exception:  # noqa: BLE001
        pass


def log_http(provider: str, model: str, t0: float, response: Any, *, feature: str | None = None) -> None:
    """For a call that hands back a response instead of raising on an error status (echo reduction): records it as a
    success below 400, and as a failure with the status and the provider's reason from 400 up."""
    status = getattr(response, "status_code", 0) or 0
    if status < 400:
        log_attempt(provider, model, t0, result=None, feature=feature)
        return

    class _HttpFailure(Exception):
        status_code = status

    failure = _HttpFailure(str(getattr(response, "text", "") or "")[:300])
    failure.response = response  # type: ignore[attr-defined]
    log_attempt(provider, model, t0, exc=failure, feature=feature)


class track:
    """`async with track("elevenlabs", "eleven_multilingual_v2", feature="text_to_speech"): ...` records the attempt
    when the block ends, and lets any error carry on to the caller untouched."""

    def __init__(self, provider: str, model: str, *, feature: str | None = None) -> None:
        self.provider, self.model, self.feature = provider, model, feature
        self._t0 = 0.0

    async def __aenter__(self) -> "track":
        self._t0 = time.perf_counter()
        return self

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        log_attempt(self.provider, self.model, self._t0, exc=exc if exc_type else None, feature=self.feature)
        return False


def note_fallback_failed(provider: str, model: str, feature: str, message: str) -> None:
    """Every provider that was tried failed, so people are seeing the failure. Counted as an error, not as a second call."""
    try:
        from app.shared.llm_health.recorder import Attempt, recorder

        recorder.record(Attempt(
            provider=provider, model=model, ok=False, latency_ms=0.0, error_class="fallback_failed", error_message=message,
            outcome="failed", count_in_rollup=False, feature=feature,
        ))
    except Exception:  # noqa: BLE001
        pass
