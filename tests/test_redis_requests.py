"""How often the personal-assistant consumer asks Redis for work. Every request counts against the Redis plan's monthly limit, so the wait is long, a failure backs off, a used-up
quota pauses for minutes, and local and test runs can switch the consumer off. No Redis is reached."""
import asyncio

from app.workers import agent_worker as aw


def test_an_idle_read_waits_long_and_failures_back_off_up_to_a_cap():
    assert aw.BLOCK_MS >= 30_000
    delays = [aw._error_delay(n, RuntimeError("connection reset")) for n in range(10)]
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert delays == sorted(delays) and max(delays) == aw.MAX_ERROR_DELAY


def test_a_used_up_request_quota_pauses_for_minutes_not_seconds():
    error = RuntimeError("max requests limit exceeded. Limit: 500000, Usage: 500000. See the docs for details")
    assert aw._error_delay(0, error) == aw.QUOTA_PAUSE_SECONDS >= 300
    assert aw._error_delay(5, error) == aw.QUOTA_PAUSE_SECONDS


async def test_the_loop_backs_off_on_failures_and_starts_again_at_one_second_after_a_good_read(monkeypatch):
    sleeps: list[float] = []
    stop = asyncio.Event()
    reads = {"n": 0}

    class FakeRedis:
        async def xreadgroup(self, *args, **kwargs):
            reads["n"] += 1
            if reads["n"] in (1, 2, 3, 5):
                raise RuntimeError("connection reset")
            if reads["n"] == 6:
                stop.set()
            return []

    async def fake_redis():
        return FakeRedis()

    async def nothing(*args, **kwargs):
        return None

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(aw, "get_redis", fake_redis)
    monkeypatch.setattr(aw, "_ensure_group", nothing)
    monkeypatch.setattr(aw, "_reclaim_stale", nothing)
    monkeypatch.setattr(aw.asyncio, "sleep", fake_sleep)
    await aw._consume_loop(stop)
    assert sleeps == [1.0, 2.0, 4.0, 1.0]            # a good read between failures starts the back-off again


async def test_the_consumer_does_not_start_or_touch_redis_when_it_is_switched_off(monkeypatch):
    touched: list[int] = []

    async def fake_redis():
        touched.append(1)
        raise AssertionError("Redis must not be used while the consumer is off")

    monkeypatch.setattr(aw.settings, "AGENT_CONSUMER_ENABLED", False)
    monkeypatch.setattr(aw, "get_redis", fake_redis)
    ctx: dict = {}
    await aw.personal_consumer_guard(ctx)
    assert "personal_consumer_task" not in ctx and touched == []


async def test_the_consumer_starts_when_it_is_switched_on(monkeypatch):
    started: list[int] = []

    async def fake_loop(stop):
        started.append(1)

    monkeypatch.setattr(aw.settings, "AGENT_CONSUMER_ENABLED", True)
    monkeypatch.setattr(aw, "_consume_loop", fake_loop)
    ctx: dict = {}
    await aw.personal_consumer_guard(ctx)
    await ctx["personal_consumer_task"]
    assert started == [1]
