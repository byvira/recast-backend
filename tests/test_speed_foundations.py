"""The things that make every request quicker: a token found in good standing is not asked about again for a short while, the calls to the chat services reuse an
open connection, and each chat message's time is logged. No Redis and no network are used."""
import asyncio
import json
import logging

import httpx

from app.channels import engine, pool, telegram
from app.channels.base import Inbound
from app.core import auth
from app.core.config import settings


class FakePipe:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def get(self, key):
        self.ops.append(("get", key))

    def exists(self, key):
        self.ops.append(("exists", key))

    async def execute(self):
        self.redis.trips += 1
        return [self.redis.data.get(k) if op == "get" else int(k in self.redis.data) for op, k in self.ops]


class FakeRedis:
    def __init__(self):
        self.data: dict = {}
        self.trips = 0

    def pipeline(self, transaction=False):
        return FakePipe(self)

    async def set(self, key, value, ex=None):
        self.data[key] = value


def _setup(monkeypatch):
    redis = FakeRedis()

    async def get_redis():
        return redis

    monkeypatch.setattr(auth, "get_redis", get_redis)
    auth._good_standing.clear()
    return redis


def _token(sid="s1"):
    return auth.create_access_token({"sub": "u1", "sid": sid})


async def test_a_token_in_good_standing_is_not_asked_about_again_within_the_window(monkeypatch):
    redis = _setup(monkeypatch)
    token = _token()
    assert await auth._revocation_flags(token, "s1") == (False, False)
    assert await auth._revocation_flags(token, "s1") == (False, False)
    assert await auth._revocation_flags(token, "s1") == (False, False)
    assert redis.trips == 1


async def test_it_is_asked_again_once_the_window_has_passed(monkeypatch):
    redis = _setup(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    token = _token()
    await auth._revocation_flags(token, "s1")
    clock[0] += auth.REVOCATION_CACHE_SECONDS - 1
    await auth._revocation_flags(token, "s1")
    assert redis.trips == 1
    clock[0] += 2
    await auth._revocation_flags(token, "s1")
    assert redis.trips == 2


async def test_a_revoked_token_is_never_remembered_as_fine(monkeypatch):
    redis = _setup(monkeypatch)
    token = _token()
    redis.data[auth._blacklist_key(token)] = "1700000000"
    assert await auth._revocation_flags(token, "s1") == (True, False)
    assert await auth._revocation_flags(token, "s1") == (True, False)
    assert redis.trips == 2


async def test_logging_a_token_out_is_seen_at_once_by_the_process_that_did_it(monkeypatch):
    _setup(monkeypatch)
    token = _token()
    assert await auth._revocation_flags(token, "s1") == (False, False)
    await auth.blacklist_token(token)
    assert (await auth._revocation_flags(token, "s1"))[0] is True


async def test_ending_a_session_is_seen_at_once_for_every_token_of_it(monkeypatch):
    _setup(monkeypatch)
    first, second = _token("s9"), _token("s9")
    other = _token("s2")
    for t, sid in ((first, "s9"), (second, "s9"), (other, "s2")):
        assert await auth._revocation_flags(t, sid) == (False, False)
    await auth.revoke_session("s9")
    assert (await auth._revocation_flags(first, "s9"))[1] is True
    assert (await auth._revocation_flags(second, "s9"))[1] is True
    assert (await auth._revocation_flags(other, "s2")) == (False, False)          # another session is untouched


async def test_what_is_remembered_never_grows_without_limit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(auth, "REVOCATION_CACHE_MAX", 3)
    for i in range(8):
        await auth._revocation_flags(_token(f"s{i}"), f"s{i}")
    assert len(auth._good_standing) <= 3


# ── the connection pool ──────────────────────────────────────────────────────

async def test_the_pool_gives_one_client_per_loop_and_a_new_one_after_it_is_closed():
    first = pool.shared(5.0)
    assert pool.shared(5.0) is first
    await pool.close_all()
    assert first.is_closed
    second = pool.shared(5.0)
    assert second is not first
    await pool.close_all()


async def test_an_adapter_with_no_client_of_its_own_uses_the_shared_pool(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "bot")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(pool, "shared", lambda timeout: client)
    adapter = telegram.TelegramAdapter()
    await adapter.working("1")
    await adapter.working("1")
    assert calls == ["/botbot/sendChatAction"] * 2
    assert not client.is_closed                         # the adapter never closes the shared pool


# ── timing ───────────────────────────────────────────────────────────────────

async def test_each_message_logs_how_long_it_took_and_how_it_ended(monkeypatch, caplog):
    async def quick(adapter, msg, **kw):
        await asyncio.sleep(0.02)
        return "plan_sent"

    class Chat:
        name = "t"

        async def send(self, *a, **k):
            return True

    monkeypatch.setattr(engine, "_handle", quick)
    with caplog.at_level(logging.INFO, logger=engine.logger.name):
        await engine.handle(Chat(), Inbound(channel="telegram", sender="1", message_id="m", kind="text", text="hi"))
    line = next(r.getMessage() for r in caplog.records if "channel message handled" in r.getMessage())
    assert "channel=telegram" in line and "outcome=plan_sent" in line and "ms=" in line
    assert json.dumps(line)                              # plain text, nothing that could not be written down
