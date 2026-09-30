"""Three things that multiplied model calls or crashed a queue, found in the 2026-09-30 logs:
1. a text too short to measure has no reading grade, and float(None) crashed the voice update every time;
2. a graph that raised inside tracing was run a second time, doubling every model call;
3. the image safety check gave up on the first rate limit. Pure tests, no network."""
import asyncio

import pytest

from app.agents.personal import persona_store
from app.core import tracing
from app.pipelines.media import image_generation as ig


# ---- 1. reading grade ---------------------------------------------------------------------------
def test_a_missing_value_keeps_the_old_one_instead_of_crashing():
    assert persona_store._ewma_scalar(7.25, None, 0.9, bootstrap=False) == 7.25
    assert persona_store._ewma_scalar(0.0, None, 0.9, bootstrap=True) == 0.0


def test_real_values_still_blend_as_before():
    assert persona_store._ewma_scalar(0.0, 6.0, 0.9, bootstrap=False) == 6.0
    assert persona_store._ewma_scalar(10.0, 20.0, 0.9, bootstrap=False) == 11.0


def test_a_missing_similarity_is_recorded_as_zero_not_a_crash():
    persona: dict = {}
    from datetime import datetime, timezone

    persona_store.append_drift_history(persona, signal_type="drift", similarity=None, severity="low", now=datetime.now(timezone.utc))  # type: ignore[arg-type]
    assert persona["drift_history"][0]["similarity"] == 0.0


# ---- 2. the graph runs once ---------------------------------------------------------------------
class _Graph:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    async def ainvoke(self, state, config=None):
        self.calls += 1
        if self.fail:
            raise ValueError("float() argument must be a string or a real number, not 'NoneType'")
        return {"ok": True}


class _Span:
    def __init__(self, url_fails: bool = False, exit_fails: bool = False):
        self.url_fails = url_fails
        self.exit_fails = exit_fails

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.exit_fails and exc[0] is None:
            raise RuntimeError("could not send the trace")
        return False

    def get_url(self):
        if self.url_fails:
            raise RuntimeError("no url")
        return "https://trace.example/run"


def _use_fake_langsmith(monkeypatch, span):
    import langsmith.run_helpers as rh

    monkeypatch.setattr(tracing, "tracing_enabled", lambda: True)
    monkeypatch.setattr(rh, "trace", lambda **kw: span)
    monkeypatch.setattr(rh, "tracing_context", lambda **kw: _Span())


def _invoke(graph):
    return asyncio.run(tracing.ainvoke_traced(graph, {}, run_name="r", agent="a"))


def test_a_graph_error_is_raised_and_the_graph_is_not_run_again(monkeypatch):
    _use_fake_langsmith(monkeypatch, _Span())
    graph = _Graph(fail=True)
    with pytest.raises(ValueError):
        _invoke(graph)
    assert graph.calls == 1


def test_a_trace_that_fails_after_a_good_run_keeps_the_result_and_runs_once(monkeypatch):
    _use_fake_langsmith(monkeypatch, _Span(exit_fails=True))
    graph = _Graph()
    result, url = _invoke(graph)
    assert result == {"ok": True} and url == ""
    assert graph.calls == 1


def test_tracing_that_cannot_start_runs_the_graph_untraced(monkeypatch):
    import langsmith.run_helpers as rh

    def broken(**kw):
        raise RuntimeError("no tracing")

    monkeypatch.setattr(tracing, "tracing_enabled", lambda: True)
    monkeypatch.setattr(rh, "tracing_context", broken)
    graph = _Graph()
    result, url = _invoke(graph)
    assert result == {"ok": True} and url == ""
    assert graph.calls == 1


def test_a_good_traced_run_returns_the_url(monkeypatch):
    _use_fake_langsmith(monkeypatch, _Span())
    result, url = _invoke(_Graph())
    assert result == {"ok": True} and url == "https://trace.example/run"


# ---- 3. the safety check tries twice --------------------------------------------------------------
def test_a_safety_check_that_hits_a_rate_limit_once_is_tried_again(monkeypatch):
    calls = {"n": 0}

    async def llm(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 rate limit")
        return "NO"

    async def no_wait(_):
        return None

    monkeypatch.setattr(ig, "call_llm", llm)
    monkeypatch.setattr(ig.asyncio, "sleep", no_wait)
    assert asyncio.run(ig._safety_verdict("a calm desk")) == "safe"
    assert calls["n"] == 2


def test_a_safety_check_that_keeps_failing_is_an_error_and_a_yes_is_unsafe(monkeypatch):
    async def always_fail(prompt, **kw):
        raise RuntimeError("429")

    async def yes(prompt, **kw):
        return "YES"

    async def no_wait(_):
        return None

    monkeypatch.setattr(ig.asyncio, "sleep", no_wait)
    monkeypatch.setattr(ig, "call_llm", always_fail)
    assert asyncio.run(ig._safety_verdict("x")) == "error"
    monkeypatch.setattr(ig, "call_llm", yes)
    assert asyncio.run(ig._safety_verdict("x")) == "unsafe"
