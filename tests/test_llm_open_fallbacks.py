"""A call answered by an open-model fallback still counts toward the workspace's daily AI usage. No network, no real keys."""
import asyncio

import httpx

from app.shared import llm, open_fallbacks as of


def _client_with(handler, monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(of.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler), **k))


def _provider():
    return of.TextProvider(name="mistral", url="https://example.test/v1/chat", key="k", model="m")


def test_a_fallback_answer_without_token_numbers_is_counted_by_its_length(monkeypatch):
    counted: list[int] = []
    monkeypatch.setattr(llm, "_record_workspace_usage", lambda tokens, calls=1: counted.append(tokens))
    _client_with(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "a" * 300}}]}), monkeypatch)
    text = asyncio.run(of._chat(_provider(), "p" * 300, "s" * 300))
    assert text == "a" * 300
    assert counted == [300], "900 characters is about 300 tokens"


def test_a_fallback_answer_that_reports_tokens_is_counted_by_them(monkeypatch):
    counted: list[int] = []
    monkeypatch.setattr(llm, "_record_workspace_usage", lambda tokens, calls=1: counted.append(tokens))
    _client_with(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 77}}),
        monkeypatch,
    )
    asyncio.run(of._chat(_provider(), "p", ""))
    assert counted == [77]
