"""The main model provider fails in every way it can and the backup models still answer. No real call: the Groq request and the
backups are stubbed, so this checks the routing only (which failure leads to which backup, and what the caller finally sees)."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from groq import APIConnectionError, APIStatusError, RateLimitError

from app.shared import llm


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://api.groq.com/test"))


def _failures():
    return {
        "rate limit": RateLimitError("slow down", response=_response(429), body=None),
        "connection": APIConnectionError(request=httpx.Request("POST", "https://api.groq.com/test")),
        "request too large": APIStatusError("too big", response=_response(413), body=None),
        "server error": APIStatusError("oops", response=_response(500), body=None),
        "unexpected": RuntimeError("something nobody planned for"),
    }


@pytest.fixture(autouse=True)
def _fresh_breaker_and_no_waiting(monkeypatch):
    monkeypatch.setattr(llm, "_groq_breaker", llm._GroqCircuitBreaker(failure_threshold=50, cooldown_seconds=1))

    async def once(attempt, label=""):
        return await attempt()

    monkeypatch.setattr(llm, "_backoff_retry", once)


@pytest.mark.parametrize("name", list(_failures()))
async def test_a_plain_call_is_answered_by_the_backup_models_whatever_the_main_failure(monkeypatch, name):
    monkeypatch.setattr(llm, "_groq_create", AsyncMock(side_effect=_failures()[name]))
    backup = AsyncMock(return_value="from the backup models")
    monkeypatch.setattr(llm, "call_llm_fallback", backup)

    assert await llm.call_llm("Write one line.") == "from the backup models"
    assert backup.await_count == 1


@pytest.mark.parametrize("name", list(_failures()))
async def test_when_the_backups_fail_too_the_caller_gets_a_clear_unavailable_error_not_a_crash(monkeypatch, name):
    monkeypatch.setattr(llm, "_groq_create", AsyncMock(side_effect=_failures()[name]))
    monkeypatch.setattr(llm, "call_llm_fallback", AsyncMock(side_effect=HTTPException(status_code=503, detail="LLM service unavailable.")))

    with pytest.raises(HTTPException) as caught:
        await llm.call_llm("Write one line.")
    assert caught.value.status_code == 503


@pytest.mark.parametrize("name", list(_failures()))
async def test_a_structured_call_is_answered_by_the_backup_models_whatever_the_main_failure(monkeypatch, name):
    monkeypatch.setattr(llm, "_groq_create", AsyncMock(side_effect=_failures()[name]))
    backup = AsyncMock(return_value={"answer": "from the backup models"})
    monkeypatch.setattr(llm, "call_llm_structured_fallback", backup)

    assert await llm.call_llm_structured("Return JSON.") == {"answer": "from the backup models"}
    assert backup.await_count == 1


async def test_a_structured_call_never_raises_even_when_everything_fails(monkeypatch):
    monkeypatch.setattr(llm, "_groq_create", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(llm, "call_llm_structured_fallback", AsyncMock(side_effect=HTTPException(status_code=503, detail="x")))
    assert await llm.call_llm_structured("Return JSON.") == {}


async def test_an_open_breaker_goes_straight_to_the_backups_without_calling_the_main_model(monkeypatch):
    breaker = llm._GroqCircuitBreaker(failure_threshold=1, cooldown_seconds=600)
    breaker.record_failure()
    monkeypatch.setattr(llm, "_groq_breaker", breaker)
    main = AsyncMock(side_effect=AssertionError("the main model must not be called while the breaker is open"))
    monkeypatch.setattr(llm, "_groq_create", main)
    monkeypatch.setattr(llm, "call_llm_fallback", AsyncMock(return_value="backup"))

    assert await llm.call_llm("Write one line.") == "backup"
    assert not main.called


async def test_the_backup_chain_moves_from_gemini_to_the_open_models(monkeypatch):
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY", "a-key")
    monkeypatch.setattr(llm, "_gemini_skip_until", 0.0)
    monkeypatch.setattr(llm, "get_gemini_client", lambda: object())
    monkeypatch.setattr(llm, "_gemini_generate", AsyncMock(side_effect=RuntimeError("Gemini is down")))
    open_models = AsyncMock(return_value="from an open model")
    monkeypatch.setattr("app.shared.open_fallbacks.open_text_fallback", open_models)

    assert await llm.call_llm_fallback("Write one line.") == "from an open model"
    assert open_models.await_count == 1


async def test_when_every_backup_is_down_the_final_error_is_503(monkeypatch):
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr("app.shared.open_fallbacks.open_text_fallback", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as caught:
        await llm.call_llm_fallback("Write one line.")
    assert caught.value.status_code == 503


async def test_a_cancelled_run_is_stopped_before_any_model_is_called(monkeypatch):
    main = AsyncMock(return_value=None)
    monkeypatch.setattr(llm, "_groq_create", main)

    async def cancelled():
        raise RuntimeError("cancelled by the member")

    llm.set_run_gate(cancelled)
    try:
        with pytest.raises(RuntimeError):
            await llm.call_llm("Write one line.")
        with pytest.raises(RuntimeError):
            await llm.call_llm_structured("Return JSON.")
    finally:
        llm.set_run_gate(None)
    assert not main.called
