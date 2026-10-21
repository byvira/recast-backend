"""The streaming call uses the same circuit breaker as the other model calls and gives up on a stalled stream. A fake stream, no network."""
import asyncio

import httpx

from app.shared import llm

_REQ = httpx.Request("POST", "http://x")


class _Chunk:
    def __init__(self, text):
        self.choices = [type("C", (), {"delta": type("D", (), {"content": text})()})()]


def _connection_error():
    return llm.APIConnectionError(request=_REQ)


def _patch(monkeypatch, create, backup=None):
    async def default_backup(*a, **k):
        return "backup answer"

    monkeypatch.setattr(llm, "get_groq_client", lambda: object())
    monkeypatch.setattr(llm, "_groq_create", create)
    monkeypatch.setattr(llm, "call_llm_fallback", backup or default_backup)


async def _drain():
    out = []
    try:
        async for piece in llm.call_llm_stream("hi"):
            out.append(piece)
    except Exception as exc:  # noqa: BLE001
        return out, getattr(exc, "status_code", None)
    return out, None


async def test_an_open_breaker_sends_the_stream_straight_to_the_fallback_chain(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("Groq must not be tried while the breaker is open")

    _patch(monkeypatch, never)
    for _ in range(3):
        llm._groq_breaker.record_failure()
    assert llm._groq_breaker.should_skip_groq()
    assert await _drain() == (["backup answer"], None)


async def test_three_failed_streams_open_the_breaker_and_a_good_one_closes_it(monkeypatch):
    async def boom(*a, **k):
        raise _connection_error()

    _patch(monkeypatch, boom)
    for _ in range(3):
        assert await _drain() == (["backup answer"], None)
    assert llm._groq_breaker.should_skip_groq(), "three failures in a row open it"

    llm._groq_breaker = llm._GroqCircuitBreaker()
    llm._groq_breaker.record_failure()
    llm._groq_breaker.record_failure()

    async def stream():
        yield _Chunk("fine")

    async def ok(*a, **k):
        return stream()

    _patch(monkeypatch, ok)
    assert await _drain() == (["fine"], None)
    assert llm._groq_breaker._consecutive_failures == 0, "a finished stream resets the count"


async def test_a_stream_that_stalls_after_output_ends_with_a_503_and_counts_as_a_failure(monkeypatch):
    monkeypatch.setattr(llm, "_STREAM_GAP_TIMEOUT_S", 0.05)

    async def stream():
        yield _Chunk("half")
        await asyncio.sleep(30)
        yield _Chunk("never")

    async def create(*a, **k):
        return stream()

    async def backup(*a, **k):
        raise AssertionError("must not be called after output began")

    _patch(monkeypatch, create, backup)
    assert await _drain() == (["half"], 503)
    assert llm._groq_breaker._consecutive_failures == 1


async def test_a_stream_that_never_starts_is_answered_by_the_fallback_chain(monkeypatch):
    monkeypatch.setattr(llm, "_STREAM_FIRST_CHUNK_TIMEOUT_S", 0.05)

    async def stream():
        await asyncio.sleep(30)
        yield _Chunk("never")

    async def create(*a, **k):
        return stream()

    _patch(monkeypatch, create)
    assert await _drain() == (["backup answer"], None)
    assert llm._groq_breaker._consecutive_failures == 1


async def test_a_refusal_of_one_request_does_not_count_against_the_breaker(monkeypatch):
    async def refuse(*a, **k):
        raise llm.APIStatusError("too large", response=httpx.Response(413, request=_REQ), body=None)

    _patch(monkeypatch, refuse)
    assert await _drain() == (["backup answer"], None)
    assert llm._groq_breaker._consecutive_failures == 0


async def test_the_fallback_chain_failing_too_ends_with_a_503(monkeypatch):
    async def boom(*a, **k):
        raise _connection_error()

    async def dead(*a, **k):
        raise RuntimeError("all down")

    _patch(monkeypatch, boom, dead)
    assert await _drain() == ([], 503)
