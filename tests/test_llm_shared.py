"""Tests for the system-level latency/robustness fixes in app/shared/llm.py:

  - reasoning_effort applied to Groq calls, with safe fallback when the
    installed SDK/API build doesn't accept the kwarg (_groq_create)
  - the circuit breaker that skips Groq entirely once it's shown enough
    consecutive failures to be considered down for the moment
  - the concurrency semaphore bounding simultaneous Groq requests

No real Groq/Gemini calls here — this suite tests the pure logic (the
breaker's state machine, the fallback-kwarg tolerance, the semaphore's
actual bounding behaviour) against fakes, following this project's own
established pattern of never calling a real LLM in the automated suite.
"""

import asyncio

import pytest

from app.shared.llm import _GroqCircuitBreaker, _groq_create, _GROQ_CONCURRENCY_LIMIT, _groq_semaphore


# ─────────────────────────────────────────────────────────────────────────────
# Circuit breaker
# ─────────────────────────────────────────────────────────────────────────────

def test_breaker_starts_closed():
    breaker = _GroqCircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    assert breaker.should_skip_groq() is False


def test_breaker_stays_closed_below_threshold():
    breaker = _GroqCircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.should_skip_groq() is False


def test_breaker_opens_at_threshold():
    breaker = _GroqCircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.should_skip_groq() is True


def test_breaker_success_resets_failure_count():
    breaker = _GroqCircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    # Only 2 consecutive since the reset — still below threshold of 3.
    assert breaker.should_skip_groq() is False


def test_breaker_half_opens_after_cooldown():
    breaker = _GroqCircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.should_skip_groq() is True  # open, inside cooldown

    import time
    time.sleep(0.08)

    # Cooldown elapsed — half-open lets exactly one trial request through.
    assert breaker.should_skip_groq() is False
    # Immediately after that, still not skipping (would re-open only on
    # another recorded failure, not just from asking twice).
    assert breaker.should_skip_groq() is False


def test_breaker_reopens_if_half_open_trial_fails():
    breaker = _GroqCircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
    breaker.record_failure()
    breaker.record_failure()

    import time
    time.sleep(0.08)

    assert breaker.should_skip_groq() is False  # half-open trial allowed
    breaker.record_failure()  # the trial failed
    assert breaker.should_skip_groq() is True  # back open


def test_breaker_closes_if_half_open_trial_succeeds():
    breaker = _GroqCircuitBreaker(failure_threshold=2, cooldown_seconds=0.05)
    breaker.record_failure()
    breaker.record_failure()

    import time
    time.sleep(0.08)

    assert breaker.should_skip_groq() is False  # half-open trial allowed
    breaker.record_success()
    assert breaker.should_skip_groq() is False
    # And failure count genuinely reset, not just coincidentally under threshold.
    breaker.record_failure()
    assert breaker.should_skip_groq() is False


# ─────────────────────────────────────────────────────────────────────────────
# _groq_create — reasoning_effort application and safe fallback
# ─────────────────────────────────────────────────────────────────────────────

class _FakeClient:
    """Records every call.create(...) invocation's kwargs."""

    def __init__(self, raise_on_reasoning_effort=None):
        self.calls = []
        self._raise_on_reasoning_effort = raise_on_reasoning_effort
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_on_reasoning_effort and "reasoning_effort" in kwargs:
            raise self._raise_on_reasoning_effort
        return "ok"


async def test_groq_create_passes_reasoning_effort_when_given():
    client = _FakeClient()
    result = await _groq_create(client, reasoning_effort="low", model="m", messages=[])
    assert result == "ok"
    assert client.calls[-1]["reasoning_effort"] == "low"


async def test_groq_create_skips_kwarg_entirely_when_none():
    client = _FakeClient()
    await _groq_create(client, reasoning_effort=None, model="m", messages=[])
    assert "reasoning_effort" not in client.calls[-1]


async def test_groq_create_falls_back_on_type_error():
    client = _FakeClient(raise_on_reasoning_effort=TypeError("unexpected keyword argument"))
    result = await _groq_create(client, reasoning_effort="low", model="m", messages=[])
    assert result == "ok"
    # First call attempted with the kwarg (and failed), second call without it.
    assert client.calls[0].get("reasoning_effort") == "low"
    assert "reasoning_effort" not in client.calls[1]


async def test_groq_create_falls_back_when_api_400s_mentioning_reasoning_effort():
    client = _FakeClient(raise_on_reasoning_effort=Exception("Invalid parameter: reasoning_effort"))
    result = await _groq_create(client, reasoning_effort="low", model="m", messages=[])
    assert result == "ok"
    assert "reasoning_effort" not in client.calls[-1]


async def test_groq_create_reraises_unrelated_exceptions():
    client = _FakeClient(raise_on_reasoning_effort=Exception("totally unrelated failure"))
    with pytest.raises(Exception, match="totally unrelated failure"):
        await _groq_create(client, reasoning_effort="low", model="m", messages=[])


# ─────────────────────────────────────────────────────────────────────────────
# Concurrency semaphore
# ─────────────────────────────────────────────────────────────────────────────

def test_semaphore_configured_with_expected_limit():
    assert _GROQ_CONCURRENCY_LIMIT >= 1
    assert _groq_semaphore._value == _GROQ_CONCURRENCY_LIMIT  # not yet acquired by anything


async def test_groq_create_actually_bounds_concurrency_via_its_own_semaphore():
    """_groq_create's real module-level _groq_semaphore must genuinely
    serialize requests beyond _GROQ_CONCURRENCY_LIMIT, not just exist
    unused — fire more concurrent calls than the limit and confirm the
    observed peak concurrency never exceeds it."""
    concurrent = 0
    max_concurrent = 0

    async def slow_create(**kwargs):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return "ok"

    client = _FakeClient()
    client.create = slow_create

    # Deliberately more concurrent callers than _GROQ_CONCURRENCY_LIMIT.
    await asyncio.gather(*(
        _groq_create(client, reasoning_effort=None, model="m", messages=[])
        for _ in range(_GROQ_CONCURRENCY_LIMIT * 3)
    ))
    assert max_concurrent <= _GROQ_CONCURRENCY_LIMIT
