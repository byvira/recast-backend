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
import contextvars
import logging
import time
from collections import defaultdict, deque
from enum import Enum
from typing import Any, AsyncIterator

import aiofiles
from fastapi import HTTPException
from google import genai
from google.genai import types
from groq import APIConnectionError, APIStatusError, AsyncGroq, RateLimitError

from app.core.config import settings
from app.core.tracing import add_run_metadata, traceable
from app.prompts.registry import load_prompt
from app.utils.jsonparser import parse_llm_json

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────

class GroqModel(str, Enum):
    # Migrated 2026-09-10: the prior llama-3.x / deepseek IDs were all
    # decommissioned by Groq (404 / 400). Current lineup:
    FAST      = "openai/gpt-oss-20b"            # simple / single-field JSON, cheap
    BALANCED  = "openai/gpt-oss-120b"           # generation, hooks, SEO, repurpose
    POWERFUL  = "openai/gpt-oss-120b"           # complex multi-step reasoning
    REASONING = "openai/gpt-oss-120b"           # deep reasoning — pass reasoning_effort="high" at the call site if a distinct tier is needed
    WHISPER   = "whisper-large-v3"              # transcription only (audio pipeline)


class GeminiModel(str, Enum):
    # Migrated 2026-09-26: gemini-2.5-flash-lite and gemini-2.5-pro both 404
    # ("no longer available to new users") with Google's own recommended
    # replacement named in the error body. gemini-2.5-flash itself returns a
    # separate 403 PERMISSION_DENIED ("Your project has been denied access.
    # Please contact support.") — a project-level access issue, not a model-
    # name problem; gemini-3.5-flash returns the identical 403, confirming
    # it's real access denial rather than another deprecated name. See
    # DEF-024 in docs/DEFERRED_AND_PARTIAL_SCOPE.md — this is a Google
    # Cloud Console issue the app owner must resolve directly with Google;
    # no model-ID change here fixes it.
    FLASH      = "gemini-3.5-flash"
    FLASH_LITE = "gemini-3.5-flash-lite"
    PRO        = "gemini-3.1-pro-preview"
    # Nano Banana — image generation, not text. Paid, no free tier
    # ($0.039/image, verified live 2026-09-25). Only ever called from
    # app.pipelines.media.image_generation's capped Cloudflare fallback.
    IMAGE      = "gemini-2.5-flash-image"


# Embedding model for the personal-assistant voice baseline.
# NOTE: the plan named "text-embedding-004"; that id 404s on the current
# Gemini API key, so we use gemini-embedding-001 truncated to 768 dims.
# At <3072 dims Gemini does NOT return a unit vector, so embed_text()
# L2-normalises before returning — required for cosine similarity to work.
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768  # provisional — 768 keeps persona docs small; revisit if drift resolution is poor


# ─────────────────────────────────────────────────────────────
# Token / call usage tracking (in-process counters)
# ─────────────────────────────────────────────────────────────

_usage: dict[str, int] = defaultdict(int)
# keys: "{provider}:{model_name}.prompt_tokens"
#       "{provider}:{model_name}.completion_tokens"
#       "{provider}:{model_name}.cached_tokens"
#       "{provider}:{model_name}.calls"
# Process-lifetime counters, not a durable history — see get_usage_stats().

# Which workspace to attribute the *next* recorded call to, for the Ops
# Dashboard's per-workspace AI usage (app/models/ai_usage.py's
# workspace_ai_usage_daily — previously scaffolded but nothing wrote to it;
# see that module's docstring). A ContextVar rather than a function
# parameter threaded through every call_llm()/call_llm_structured() call
# site (a dozen+ pipelines) — set it once per request/agent-run via
# usage_workspace() and every LLM call inside that scope is attributed
# automatically, task-isolated so concurrent requests from different
# workspaces never cross-contaminate.
_current_workspace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_workspace_id", default=None
)


class usage_workspace:
    """Context manager: attribute every LLM call made inside the block to
    *workspace_id* in workspace_ai_usage_daily. Usage:

        async with usage_workspace(workspace_id):
            await call_llm_structured(...)

    Safe to nest/omit — calls outside any usage_workspace() block still
    count toward the process-lifetime totals in _usage, just not toward any
    workspace's daily rollup.
    """

    def __init__(self, workspace_id: str | None) -> None:
        self._workspace_id = workspace_id
        self._token: contextvars.Token | None = None

    def __enter__(self) -> None:
        self._token = _current_workspace_id.set(self._workspace_id)

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _current_workspace_id.reset(self._token)

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(self, *exc_info: object) -> None:
        self.__exit__(*exc_info)


def set_usage_workspace(workspace_id: str | None) -> None:
    """Fire-and-forget alternative to usage_workspace() for a large
    function where wrapping the whole body in a `with` block would mean
    reindenting it (e.g. run_text_pipeline, "main entry point for all text
    generation" — angles/generator/repurpose/chips/hook_agent/normalizer/
    scorer/seo all get attributed to *workspace_id* just by this one call at
    its top, no signature changes needed in any of those files).

    No matching reset — safe here specifically because each real caller
    (an HTTP/SSE request, or one iteration of a campaign's day-loop, which
    is always the same workspace_id per campaign) runs in its own asyncio
    Task, and the Task (and this ContextVar's value with it) is discarded
    once that request/run finishes. Do not call this from a long-lived
    worker loop that reuses one Task across multiple different workspaces
    without an intervening call to reset it — use usage_workspace() there
    instead (see Odette's reason_node/synthesize_node for that pattern).
    """
    _current_workspace_id.set(workspace_id)


def _record_workspace_usage(total_tokens: int, calls: int = 1) -> None:
    """Fire-and-forget increment of today's workspace_ai_usage_daily row, if
    usage_workspace() set a workspace for the current task. Never awaited by
    the caller — a Mongo write must not add latency to an LLM response, same
    principle as assist.py's cached_nudge 150ms-timeout comment."""
    workspace_id = _current_workspace_id.get()
    if not workspace_id or total_tokens <= 0:
        return

    async def _write() -> None:
        try:
            from datetime import date as _date

            from app.db.mongo import workspace_ai_usage_daily

            today = _date.today().isoformat()
            await workspace_ai_usage_daily.update_one(
                {"_id": f"{workspace_id}:{today}"},
                {
                    "$inc": {"tokens_used": total_tokens, "calls": calls},
                    "$setOnInsert": {"id": f"{workspace_id}:{today}", "workspace_id": workspace_id, "date": today},
                },
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001 — usage tracking must never break generation
            logger.debug("workspace usage write skipped: %s", exc)

    try:
        asyncio.get_running_loop().create_task(_write())
    except RuntimeError:
        pass  # no running loop (e.g. a script/test context) — skip silently


def _record_usage(model_name: str, usage: Any, *, provider: str = "groq") -> None:
    """Accumulate token counts from a Groq- or Gemini-shaped usage object.

    cached_tokens (Groq: usage.prompt_tokens_details.cached_tokens; Gemini:
    usage_metadata.cached_content_token_count) is the only ground truth that
    each provider's automatic prompt caching — a repeated prefix is cached
    server-side with no cache_control markers to set on either provider —
    is actually hitting. Without this, "is caching working" was
    unanswerable from this codebase; nothing surfaced it before.
    """
    if usage is None:
        return
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    cached_tokens = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    if prompt_tokens is None:  # Gemini's GenerateContentResponseUsageMetadata shape
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        completion_tokens = getattr(usage, "candidates_token_count", 0) or 0
        cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0
    completion_tokens = completion_tokens or 0

    key = f"{provider}:{model_name}"
    _usage[f"{key}.calls"]             += 1
    _usage[f"{key}.prompt_tokens"]     += prompt_tokens
    _usage[f"{key}.completion_tokens"] += completion_tokens
    _usage[f"{key}.cached_tokens"]     += cached_tokens

    _record_workspace_usage(prompt_tokens + completion_tokens)


def get_usage_stats() -> dict[str, int]:
    """Return a snapshot of accumulated token / call counters.

    Divide ``{provider}:{model}.cached_tokens`` by ``.prompt_tokens`` for a
    per-model cache hit rate — near-zero on a model with a large, mostly-
    static prompt (e.g. Odette's supervisor reasoning) means something
    upstream is breaking the shared prefix (build_odette_system / TOOL_SPECS
    must stay byte-identical across calls for the same language to keep
    hitting cache).
    """
    return dict(_usage)


# ─────────────────────────────────────────────────────────────
# Latency + recent-error tracking — process-lifetime, for the Ops
# Dashboard's LLM health page. Bounded (deque maxlen) so this never grows
# unbounded on a long-running process.
# ─────────────────────────────────────────────────────────────

_LATENCY_WINDOW = 200
_latency: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_LATENCY_WINDOW))

_ERROR_LOG_SIZE = 50
_recent_errors: deque[dict[str, Any]] = deque(maxlen=_ERROR_LOG_SIZE)


def _record_latency(provider: str, model_name: str, elapsed_ms: float) -> None:
    _latency[f"{provider}:{model_name}"].append(elapsed_ms)


def _record_error(provider: str, model_name: str, kind: str, message: str) -> None:
    _recent_errors.appendleft({
        "at": time.time(),
        "provider": provider,
        "model": model_name,
        "kind": kind,
        "message": message[:300],
    })


def get_latency_stats() -> dict[str, dict[str, float]]:
    """Per-{provider}:{model} count/avg/p95 latency (ms), over the last
    _LATENCY_WINDOW calls. p95 is a simple sorted-index estimate — fine at
    this sample size, not a claim of statistical rigor."""
    stats: dict[str, dict[str, float]] = {}
    for key, samples in _latency.items():
        if not samples:
            continue
        ordered = sorted(samples)
        p95_idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
        stats[key] = {
            "count": len(ordered),
            "avg_ms": round(sum(ordered) / len(ordered), 1),
            "p95_ms": round(ordered[p95_idx], 1),
        }
    return stats


def get_recent_errors() -> list[dict[str, Any]]:
    """Most-recent-first, up to _ERROR_LOG_SIZE entries."""
    return list(_recent_errors)


def get_circuit_breaker_status() -> dict[str, Any]:
    return {
        "provider": "groq",
        "open": _groq_breaker._opened_at is not None,
        "consecutive_failures": _groq_breaker._consecutive_failures,
    }


# ─────────────────────────────────────────────────────────────
# Concurrency governor — bounds simultaneous Groq requests
# ─────────────────────────────────────────────────────────────
#
# GROQ_TPM_LIMIT is small (8000 on the current free tier) and a single
# generation call alone can request up to 4000 max_tokens — a handful of
# simultaneous requests (e.g. a few platforms generating in parallel, or a
# couple of users overlapping) can exhaust the whole per-minute budget on
# their own. When that happens the cost isn't "a bit slower" — it's
# RateLimitError triggering _backoff_retry's 2s/4s/8s sleep cascade per
# request. In-process only (see the module docstring's note on cross-
# instance limits — this governs one worker's concurrency, not the
# account's real ceiling across every instance sharing it): bounding how
# many requests this process fires at once turns "race each other into a
# rate limit" into "queue briefly," which is a smaller and far more
# predictable cost.
_GROQ_CONCURRENCY_LIMIT = 4
_groq_semaphore = asyncio.Semaphore(_GROQ_CONCURRENCY_LIMIT)


# ─────────────────────────────────────────────────────────────
# Circuit breaker — stops paying the full retry/backoff tax on every
# request once Groq is known-bad for the moment
# ─────────────────────────────────────────────────────────────
#
# Without this, a genuinely degraded (not fully down) Groq costs *every*
# request the same full price: timeout + SDK retries + the 2s/4s/8s
# backoff, before finally falling back to Gemini. That's fine for one
# unlucky request; it's a real, cumulative latency tax when it's actually
# Groq having a bad few minutes. Trip after a run of consecutive
# connection/rate-limit failures, skip straight to Gemini for a cooldown,
# then let one trial request through to check recovery (half-open) rather
# than guessing.
class _GroqCircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 30.0):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold and self._opened_at is None:
            self._opened_at = time.time()
            logger.warning(
                "Groq circuit breaker OPEN after %d consecutive failures — "
                "routing straight to Gemini for %.0fs",
                self._consecutive_failures, self._cooldown_seconds,
            )

    def should_skip_groq(self) -> bool:
        """True while the breaker is open and still within its cooldown.

        Flips back to allowing one trial request (half-open) once the
        cooldown elapses — record_success()/record_failure() from that
        trial then decide whether to fully close or re-open.
        """
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at >= self._cooldown_seconds:
            logger.info("Groq circuit breaker half-open — allowing a trial request")
            self._opened_at = None  # let this request through; outcome decides next state
            self._consecutive_failures = self._failure_threshold - 1
            return False
        return True


_groq_breaker = _GroqCircuitBreaker()


# ─────────────────────────────────────────────────────────────
# Clients — singletons
# ─────────────────────────────────────────────────────────────

_groq_client: AsyncGroq | None = None
_gemini_client: genai.Client | None = None


def get_groq_client() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        client = AsyncGroq(
            api_key=settings.GROQ_API_KEY,
            timeout=30.0,
            max_retries=2,
        )
        # Patch for LangSmith tracing (no-op when tracing is disabled). Every
        # chat.completions.create through this singleton — llm.py helpers and the
        # supervisor's raw ReAct loop alike — then becomes a traced LLM run.
        try:
            from app.core.tracing import wrap_groq
            client = wrap_groq(client)
        except Exception:  # noqa: BLE001 — tracing must never break the client
            pass
        _groq_client = client
    return _groq_client


def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _gemini_client


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

_JSON_SYSTEM_SUFFIX = load_prompt("fragments/json_output_contract")

# Approximate chars-per-token for prompt budget estimation. Deliberately
# conservative (overestimates token count) because Groq's real accounting
# is what actually fails a request — an estimate that runs a little high is
# just a slightly smaller max_tokens ask; an estimate that runs low is a real
# 413 "request too large" error with no graceful degradation. Tightened from
# 3.5 after that exact failure mode showed up live: a request the old
# estimate cleared still landed over the account's true per-minute ceiling.
_CHARS_PER_TOKEN = 3.0

# Extra headroom below GROQ_TPM_LIMIT, on top of the (already-conservative)
# token estimate above. Two independent reasons this exists, not one:
#   1. The char-based estimate is still an approximation, not a real
#      tokenizer count — this absorbs normal estimation error.
#   2. Concurrent requests: Groq's per-minute budget is shared across every
#      call this process (or others on the same account) makes in the same
#      60s window, so a second request can eat into the budget between when
#      we estimate and when Groq actually receives ours.
_TPM_SAFETY_MARGIN = 500


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _safe_max_tokens(prompt: str, system: str, ceiling: int) -> int:
    """
    Cap max_tokens so prompt + requested output stays under this account's
    real tokens-per-minute limit (settings.GROQ_TPM_LIMIT) — Groq counts
    both toward the same per-minute budget, not just the model's context
    window, so a big prompt with a big max_tokens ask can 413 even on a
    model whose context window is much larger than either number alone.

    GROQ_TPM_LIMIT is a setting, not a hardcoded constant, so upgrading the
    Groq account tier later is a config change, not a code change.
    """
    used = _estimate_tokens(prompt) + _estimate_tokens(system)
    available = settings.GROQ_TPM_LIMIT - used - _TPM_SAFETY_MARGIN
    return max(512, min(ceiling, available))


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


def _flatten_chat(messages: list[dict[str, str]]) -> str:
    """Turn a multi-turn message history into one prompt string for the
    Gemini fallback, which takes a single prompt, not a message list.
    Degraded (loses native multi-turn structure) but only used when Groq's
    chat path has already failed outright — a readable transcript beats no
    response at all.
    """
    return "\n\n".join(f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in messages)


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


async def _groq_create(client: AsyncGroq, *, reasoning_effort: str | None, **kwargs: Any) -> Any:
    """
    client.chat.completions.create(), with reasoning_effort applied when a
    caller asks for one — and silently omitted if the installed SDK/API
    build doesn't accept it, same tolerance pattern already proven in
    app.agents.supervisor.nodes._groq_chat.

    Why this exists at all: gpt-oss-120b is a reasoning model that, left at
    its own default reasoning depth, was observed live (2026-09-15) to spend
    its *entire* max_tokens budget on hidden reasoning_tokens for a plain,
    short, English generation prompt — not just the previously-documented
    non-English case — leaving zero tokens for visible output and either
    truncating to nothing or failing Groq's own JSON validation outright.
    reasoning_effort="low" resolved it in side-by-side testing: 1.77s,
    finish_reason=stop, real output, vs. an outright 400 with no
    reasoning_effort set on the identical prompt. Every call site that
    generates content-shaped output (not the supervisor's own multi-step
    rule reasoning, which already sets "high" itself) should default low.

    Also acquires _groq_semaphore for the duration of the request — see
    that name's docstring for why every Groq call funnels through one
    bounded gate rather than firing unbounded.
    """
    model_name = kwargs.get("model", "unknown")
    t0 = time.perf_counter()
    try:
        async with _groq_semaphore:
            if reasoning_effort is None:
                result = await client.chat.completions.create(**kwargs)
            else:
                try:
                    result = await client.chat.completions.create(reasoning_effort=reasoning_effort, **kwargs)
                except TypeError:
                    result = await client.chat.completions.create(**kwargs)
                except Exception as exc:  # noqa: BLE001 - some Groq builds 400 instead of TypeError
                    if "reasoning_effort" in str(exc):
                        result = await client.chat.completions.create(**kwargs)
                    else:
                        raise
    except Exception as exc:
        _record_error("groq", str(model_name), exc.__class__.__name__, str(exc))
        raise
    _record_latency("groq", str(model_name), (time.perf_counter() - t0) * 1000)
    return result


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
    reasoning_effort: str | None = "low",
) -> str:
    """
    Plain text generation via Groq.

    Retries with exponential back-off on RateLimitError (up to 3 attempts),
    then falls back to FAST model, then raises HTTP 503.

    reasoning_effort defaults to "low" — see _groq_create's docstring for
    why: gpt-oss-120b at its default reasoning depth can burn the entire
    token budget on hidden reasoning for an ordinary generation prompt,
    producing empty output. Pass None to use the API's own default (only
    the supervisor's own deliberately deep multi-step reasoning calls
    should ever need more than "low" here), or "high" for a task that
    genuinely needs deeper deliberation.
    """
    client   = get_groq_client()
    messages = _build_messages(prompt, system)
    capped   = _safe_max_tokens(prompt, system, max_tokens)

    async def _complete(mdl: GroqModel) -> str:
        resp = await _groq_create(
            client,
            reasoning_effort=reasoning_effort,
            model=mdl.value,
            messages=messages,
            temperature=temperature,
            max_tokens=capped,
        )
        _log_if_truncated(resp, f"call_llm({mdl.name})")
        _record_usage(mdl.value, resp.usage)
        return _raw_text(resp)

    # Circuit breaker: if Groq has already failed enough times in a row to
    # be considered down for the moment, skip the timeout+retries+backoff
    # tax entirely and go straight to the already-known-working fallback.
    if _groq_breaker.should_skip_groq():
        try:
            result = await call_llm_fallback(prompt=prompt, system=system)
            return result
        except Exception as exc:
            logger.error("Gemini fallback also failed (breaker open): %s", exc)
            raise HTTPException(status_code=503, detail="LLM service unavailable.")

    try:
        result = await _backoff_retry(
            lambda: _complete(model),
            label=f"call_llm({model.name})",
        )
        _groq_breaker.record_success()
        return result

    except RateLimitError:
        logger.warning("Groq rate limit persists — falling back to FAST model")
        try:
            result = await _complete(GroqModel.FAST)
            _groq_breaker.record_success()
            return result
        except Exception:
            _groq_breaker.record_failure()
            logger.warning("Groq FAST fallback also failed — falling back to Gemini")
            try:
                return await call_llm_fallback(prompt=prompt, system=system)
            except Exception as exc:
                logger.error("Gemini fallback also failed: %s", exc)
                raise HTTPException(
                    status_code=503,
                    detail="LLM rate limit. Please try again in 60 seconds.",
                )

    except APIConnectionError as exc:
        _groq_breaker.record_failure()
        logger.error("Groq connection error: %s — falling back to Gemini", exc)
        try:
            return await call_llm_fallback(prompt=prompt, system=system)
        except Exception as fallback_exc:
            logger.error("Gemini fallback also failed: %s", fallback_exc)
            raise HTTPException(status_code=503, detail="LLM service unavailable.")

    except APIStatusError as exc:
        # Distinct from RateLimitError (429, retrying/waiting can fix it):
        # a 413 "request too large" means THIS request's prompt + max_tokens
        # already exceeds the account's per-minute ceiling, so retrying the
        # same request on Groq would just 413 again. Route straight to
        # Gemini, which has its own, independent (and much larger) budget.
        # Not counted as a breaker failure — this is a property of this one
        # request's size, not evidence Groq itself is degraded.
        logger.warning(
            "Groq returned %s (%s) — not a rate limit that retrying fixes, "
            "falling back to Gemini",
            exc.status_code, exc.__class__.__name__,
        )
        try:
            return await call_llm_fallback(prompt=prompt, system=system)
        except Exception as fallback_exc:
            logger.error("Gemini fallback also failed: %s", fallback_exc)
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
    reasoning_effort: str | None = "low",
) -> AsyncIterator[str]:
    """
    Streaming plain text generation via Groq.

    Yields text chunks as they arrive. Use for SSE / WebSocket endpoints.

    Usage:
        async for chunk in call_llm_stream(prompt):
            await websocket.send_text(chunk)

    reasoning_effort defaults to "low" — same reasoning-token-budget risk
    as call_llm/call_llm_structured; see _groq_create's docstring.
    """
    client   = get_groq_client()
    messages = _build_messages(prompt, system)
    capped   = _safe_max_tokens(prompt, system, max_tokens)

    try:
        stream = await _groq_create(
            client,
            reasoning_effort=reasoning_effort,
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
    reasoning_effort: str | None = "low",
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

    reasoning_effort defaults to "low" — confirmed via live side-by-side
    testing (2026-09-15) that leaving this unset lets gpt-oss-120b spend
    its entire max_tokens budget on hidden reasoning_tokens even for a
    short, plain-English generation prompt, producing empty/truncated
    output — exactly the failure this function is supposed to hand back
    as {} for the caller to fall back from, except here the fallback was
    firing on ordinary requests, not genuine failures. "low" produced
    complete, correctly-parsed JSON in a fraction of the time. See
    _groq_create's docstring for the measured numbers.
    """
    client   = get_groq_client()
    messages = _build_messages(prompt, system, json_mode=True)
    capped   = _safe_max_tokens(prompt, system, max_tokens)

    async def _complete_and_parse(mdl: GroqModel) -> dict[str, Any]:
        resp = await _groq_create(
            client,
            reasoning_effort=reasoning_effort,
            model=mdl.value,
            messages=messages,
            temperature=0.3,
            max_tokens=capped,
        )
        _log_if_truncated(resp, f"call_llm_structured({mdl.name})")
        _record_usage(mdl.value, resp.usage)
        return parse_llm_json(_raw_text(resp))

    # Circuit breaker — see call_llm()'s identical check for why: skip the
    # timeout+retries+backoff tax entirely once Groq has already shown
    # enough consecutive failures to be considered down for the moment.
    if _groq_breaker.should_skip_groq():
        try:
            return await call_llm_structured_fallback(prompt=prompt, system=system)
        except Exception as exc:
            logger.error("Gemini structured fallback also failed (breaker open): %s", exc)
            return {}

    try:
        result = await _backoff_retry(
            lambda: _complete_and_parse(model),
            label=f"call_llm_structured({model.name})",
        )
        _groq_breaker.record_success()
        return result

    except RateLimitError:
        _groq_breaker.record_failure()
        logger.warning("Groq structured call rate limit persisted after retries — falling back to Gemini")
        try:
            return await call_llm_structured_fallback(prompt=prompt, system=system)
        except Exception as exc:
            logger.error("Gemini structured fallback also failed: %s", exc)
            return {}

    except APIConnectionError as exc:
        _groq_breaker.record_failure()
        logger.error("Groq connection error: %s — falling back to Gemini", exc)
        try:
            return await call_llm_structured_fallback(prompt=prompt, system=system)
        except Exception as fallback_exc:
            logger.error("Gemini structured fallback also failed: %s", fallback_exc)
            return {}

    except APIStatusError as exc:
        # See call_llm()'s identical branch: a 413 means this request's
        # prompt + max_tokens already exceeds the account's per-minute
        # ceiling — retrying on Groq would just 413 again, so go straight
        # to Gemini's independent budget instead. Not counted as a breaker
        # failure — a property of this request's size, not Groq health.
        logger.warning(
            "Groq returned %s (%s) — not a rate limit that retrying fixes, "
            "falling back to Gemini",
            exc.status_code, exc.__class__.__name__,
        )
        try:
            return await call_llm_structured_fallback(prompt=prompt, system=system)
        except Exception as fallback_exc:
            logger.error("Gemini structured fallback also failed: %s", fallback_exc)
            return {}

    except Exception as exc:
        logger.error("Unexpected call_llm_structured error: %s", exc)
        return {}


async def call_llm_chat(
    messages: list[dict[str, str]],
    system: str = "",
    model: GroqModel = GroqModel.BALANCED,
    max_tokens: int = 2500,
) -> str:
    """
    Conversational generation with full message history.

    Messages format:
      [
        {"role": "user",      "content": "make this punchier"},
        {"role": "assistant", "content": "previous refined version"},
        {"role": "user",      "content": "now shorten it by half"},
      ]

    System prompt is prepended as a system message.
    Brand context and banned words injected via system by the caller.

    Used for: iterative content refinement per piece.
    Each turn refines the previous version.
    Full history sent on every turn for context continuity.
    """
    client = get_groq_client()

    full_messages: list[dict[str, str]] = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    capped = _safe_max_tokens(
        prompt=messages[-1]["content"] if messages else "",
        system=system,
        ceiling=max_tokens,
    )

    try:
        return await _backoff_retry(
            lambda: _chat_complete(client, model, full_messages, capped),
            label=f"call_llm_chat({model.name})",
        )
    except RateLimitError:
        logger.warning("Groq rate limit on chat — falling back to FAST model")
        try:
            return await _chat_complete(client, GroqModel.FAST, full_messages, capped)
        except Exception:
            logger.warning("Groq FAST fallback also failed on chat — falling back to Gemini")
            try:
                return await call_llm_fallback(prompt=_flatten_chat(messages), system=system)
            except Exception as exc:
                logger.error("Gemini fallback also failed on chat: %s", exc)
                raise HTTPException(
                    status_code=503,
                    detail="LLM rate limit. Please try again in 60 seconds.",
                )
    except APIConnectionError as exc:
        logger.error("Groq connection error in chat: %s — falling back to Gemini", exc)
        try:
            return await call_llm_fallback(prompt=_flatten_chat(messages), system=system)
        except Exception as fallback_exc:
            logger.error("Gemini fallback also failed on chat: %s", fallback_exc)
            raise HTTPException(status_code=503, detail="LLM service unavailable.")
    except APIStatusError as exc:
        # Same reasoning as call_llm()'s branch: a 413 means this request's
        # prompt + max_tokens already exceeds the account's per-minute
        # ceiling — retrying on Groq would just 413 again.
        logger.warning(
            "Groq returned %s (%s) on chat — falling back to Gemini",
            exc.status_code, exc.__class__.__name__,
        )
        try:
            return await call_llm_fallback(prompt=_flatten_chat(messages), system=system)
        except Exception as fallback_exc:
            logger.error("Gemini fallback also failed on chat: %s", fallback_exc)
            raise HTTPException(status_code=503, detail="LLM service unavailable.")
    except Exception as exc:
        logger.error("Unexpected call_llm_chat error: %s", exc)
        raise HTTPException(status_code=500, detail="Unexpected error during chat.")


async def _chat_complete(
    client: AsyncGroq,
    model: GroqModel,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> str:
    """Internal helper — single chat completion call."""
    resp = await client.chat.completions.create(
        model=model.value,
        messages=messages,
        temperature=0.7,
        max_tokens=max_tokens,
    )
    _log_if_truncated(resp, f"call_llm_chat({model.name})")
    _record_usage(model.value, resp.usage)
    return _raw_text(resp)

# ─────────────────────────────────────────────────────────────
# 4. Vision / image analysis — Gemini only
# ─────────────────────────────────────────────────────────────

def _record_gemini_usage(response: Any, model_name: str = "unknown") -> None:
    """Attach Gemini token counts to the current LangSmith run, if any, and
    to the same process-lifetime _usage counters Groq calls feed (provider
    "gemini") — previously Gemini calls (vision, embeddings, fallback) were
    entirely invisible to get_usage_stats()."""
    um = getattr(response, "usage_metadata", None)
    if um is None:
        return
    add_run_metadata(
        prompt_tokens=getattr(um, "prompt_token_count", None),
        completion_tokens=getattr(um, "candidates_token_count", None),
        total_tokens=getattr(um, "total_token_count", None),
    )
    _record_usage(model_name, um, provider="gemini")


async def _gemini_generate(model_name: str, fn) -> Any:
    """Latency + error tracking around a Gemini call, mirroring _groq_create's
    treatment of the Groq side — the single place every call_vision /
    call_llm_fallback / embed_text call funnels through."""
    t0 = time.perf_counter()
    try:
        result = await fn()
    except Exception as exc:
        _record_error("gemini", model_name, exc.__class__.__name__, str(exc))
        raise
    _record_latency("gemini", model_name, (time.perf_counter() - t0) * 1000)
    return result


@traceable(run_type="llm", name="gemini.vision")
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
    add_run_metadata(gemini_model=model.value, mime_type=mime_type, image_bytes=len(image_bytes or b""))
    try:
        response = await _gemini_generate(model.value, lambda: loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=model.value,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt,
                ],
            ),
        ))
        _record_gemini_usage(response, model.value)
        return response.text or ""

    except Exception as exc:
        logger.error("Gemini vision call failed: %s", exc)
        return ""


# ─────────────────────────────────────────────────────────────
# 4b. Text embeddings — Gemini (personal-assistant voice baseline)
# ─────────────────────────────────────────────────────────────

@traceable(run_type="embedding", name="gemini.embed_text")
async def embed_text(
    text: str,
    *,
    task_type: str = "SEMANTIC_SIMILARITY",
    dim: int = EMBED_DIM,
) -> list[float]:
    """Return an L2-normalised embedding for *text* via Gemini.

    Groq has no embeddings endpoint, so this always uses Gemini. Used by the
    Layer-1 personal assistant to build and compare a member's voice baseline.
    Returns ``[]`` on failure — callers must treat an empty vector as
    "no embedding available" and skip drift scoring, never crash.
    """
    from google.genai import types  # local import — keeps module load cheap

    snippet = (text or "").strip()[:8000]
    if not snippet:
        return []

    client = get_gemini_client()
    loop = asyncio.get_running_loop()
    add_run_metadata(embed_model=EMBED_MODEL, embed_dim=dim, task_type=task_type, chars=len(snippet))
    try:
        resp = await _gemini_generate(EMBED_MODEL, lambda: loop.run_in_executor(
            None,
            lambda: client.models.embed_content(
                model=EMBED_MODEL,
                contents=snippet,
                config=types.EmbedContentConfig(
                    output_dimensionality=dim,
                    task_type=task_type,
                ),
            ),
        ))
        values = list(resp.embeddings[0].values)
        _record_gemini_usage(resp, EMBED_MODEL)
    except Exception as exc:
        logger.error("embed_text failed: %s", exc)
        return []

    # L2-normalise — Gemini only returns unit vectors at the full 3072 dims.
    norm = sum(v * v for v in values) ** 0.5
    if norm == 0.0:
        return []
    return [v / norm for v in values]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors. Assumes (but does not require) unit
    length. Returns 0.0 if either vector is empty or degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


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

@traceable(run_type="llm", name="gemini.fallback")
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
    add_run_metadata(gemini_model=model.value)

    try:
        response = await _gemini_generate(model.value, lambda: loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=model.value,
                contents=full_prompt,
            ),
        ))
        _record_gemini_usage(response, model.value)
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