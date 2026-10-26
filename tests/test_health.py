"""Tests for the /health/ready deep readiness check.

The readiness route calls cached_llm_health_check(), which makes real Groq/Gemini calls, so it is monkeypatched here
same as every other external-service call in this suite — no real LLM call
in the automated tests.
"""

import app.main as main_module


async def test_health_shallow_is_always_ok(api_client):
    res = await api_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


async def test_health_ready_ok_when_everything_healthy(api_client, monkeypatch):
    async def fake_llm_health_check():
        return {
            "groq": {"status": "ok", "latency_ms": 100},
            "gemini": {"status": "ok", "latency_ms": 120},
        }

    monkeypatch.setattr(main_module, "cached_llm_health_check", fake_llm_health_check)

    res = await api_client.get("/health/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["llm_degraded"] is False
    assert body["llm"]["groq"]["status"] == "ok"


async def test_health_ready_stays_200_when_llm_degraded_but_db_ok(api_client, monkeypatch):
    """An LLM outage alone must not flip overall readiness to 503 — most of
    the app (auth, workspace, invites, settings) works fine without it."""

    async def fake_llm_health_check():
        return {
            "groq": {"status": "error", "detail": "rate limited"},
            "gemini": {"status": "ok", "latency_ms": 120},
        }

    monkeypatch.setattr(main_module, "cached_llm_health_check", fake_llm_health_check)

    res = await api_client.get("/health/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["llm_degraded"] is True
    assert body["llm"]["groq"]["status"] == "error"


async def test_health_ready_503_when_mongo_down(api_client, monkeypatch):
    class _FailingCollectionCommand:
        async def command(self, *_a, **_kw):
            raise RuntimeError("mongo unreachable")

    class _FailingClient:
        def get_default_database(self):
            return _FailingCollectionCommand()

    async def fake_llm_health_check():
        return {"groq": {"status": "ok"}, "gemini": {"status": "ok"}}

    monkeypatch.setattr(main_module, "get_mongo_client", lambda: _FailingClient())
    monkeypatch.setattr(main_module, "cached_llm_health_check", fake_llm_health_check)

    res = await api_client.get("/health/ready")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "not ready"
    assert any("mongo" in p for p in body["problems"])


async def test_cached_llm_health_check_pings_once_within_the_window(monkeypatch):
    """A monitor hitting the readiness route every minute must not spend provider allowance each time."""
    import app.shared.llm as llm

    calls = 0

    async def fake_ping():
        nonlocal calls
        calls += 1
        return {"groq": {"status": "ok"}, "gemini": {"status": "ok"}}

    monkeypatch.setattr(llm, "llm_health_check", fake_ping)
    monkeypatch.setattr(llm, "_health_cache", None)
    first = await llm.cached_llm_health_check()
    second = await llm.cached_llm_health_check()
    assert first == second
    assert calls == 1
    # past the window it pings again
    await llm.cached_llm_health_check(max_age_s=0)
    assert calls == 2
