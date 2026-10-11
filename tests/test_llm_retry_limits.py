"""The retry layers have limits: the same model is not asked twice for nothing, and one call cannot wait forever. Stubbed, no network."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from groq import APIConnectionError, RateLimitError

from app.shared import llm


def _rate_limited() -> RateLimitError:
    return RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com/t")), body=None)


async def test_a_rate_limited_fast_model_is_not_asked_again_before_the_backups(monkeypatch):
    monkeypatch.setattr(llm, "_groq_breaker", llm._GroqCircuitBreaker(failure_threshold=50, cooldown_seconds=1))

    async def once(attempt, label="", **kw):
        return await attempt()

    monkeypatch.setattr(llm, "_backoff_retry", once)
    create = AsyncMock(side_effect=_rate_limited())
    monkeypatch.setattr(llm, "_groq_create", create)
    monkeypatch.setattr(llm, "call_llm_fallback", AsyncMock(return_value="from the backup models"))

    assert await llm.call_llm("Write one line.", model=llm.GroqModel.FAST) == "from the backup models"
    assert create.await_count == 1, "the FAST model is not retried as its own fallback"


async def test_a_call_that_never_answers_gives_up_at_the_deadline_and_looks_like_a_lost_connection():
    async def hangs():
        await asyncio.sleep(30)

    with pytest.raises(APIConnectionError):
        await llm._backoff_retry(hangs, label="test", deadline_s=0.05)


async def test_waiting_between_retries_counts_toward_the_same_deadline():
    calls = 0

    async def always_limited():
        nonlocal calls
        calls += 1
        raise _rate_limited()

    with pytest.raises(APIConnectionError):
        await llm._backoff_retry(always_limited, label="test", base_delay=100.0, deadline_s=0.2)
    assert calls == 1, "the 100 second wait was cut short by the deadline, not slept through"


def test_tamil_text_counts_for_far_more_tokens_than_the_same_length_of_english():
    english = "a" * 300
    tamil = "\u0b85" * 300
    assert llm._estimate_tokens(tamil) > 2 * llm._estimate_tokens(english)
    assert llm._estimate_tokens("") == 1


async def test_private_content_is_never_passed_to_the_backup_providers(monkeypatch):
    from fastapi import HTTPException

    gemini_called = []
    monkeypatch.setattr(llm.settings, "GEMINI_API_KEY", "x", raising=False)
    monkeypatch.setattr(llm, "get_gemini_client", lambda: gemini_called.append(1) or object())
    async with llm.private_content():
        try:
            await llm.call_llm_fallback("a member's private message")
            raised = False
        except HTTPException as exc:
            raised = exc.status_code == 503
    assert raised and not gemini_called, "no backup provider was reached"


def test_the_support_assistant_bills_the_platform_not_the_member():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "app" / "api" / "v1" / "support_assistant.py").read_text(encoding="utf-8")
    assert "usage_workspace(USAGE_BUCKET)" in source and "set_usage_workspace(ctx.workspace_id)" not in source
