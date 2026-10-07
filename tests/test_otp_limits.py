"""OTP guesses and send limits, with Redis replaced by an in-memory stand-in."""

import asyncio

import pytest
from fastapi import HTTPException

from app.core import otp
from app.core.config import settings


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = str(value)

    async def get(self, key):
        return self.data.get(key)

    async def exists(self, key):
        return 1 if key in self.data else 0

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    async def expire(self, key, seconds):
        return True

    async def ttl(self, key):
        return 5

    async def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    def pipeline(self):
        return FakePipe(self)


class FakePipe:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def set(self, *a, **k):
        self.ops.append(("set", a, k))

    def delete(self, *a):
        self.ops.append(("delete", a, {}))

    def incr(self, *a):
        self.ops.append(("incr", a, {}))

    def expire(self, *a):
        pass

    async def execute(self):
        for name, a, k in self.ops:
            await getattr(self.redis, name)(*a, **k)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def get_redis():
        return fake

    monkeypatch.setattr(otp, "get_redis", get_redis)
    return fake


def run(coro):
    return asyncio.run(coro)


def test_five_wrong_guesses_cancel_the_code_but_never_lock_the_address(redis):
    code = run(otp.generate_otp("a@b.com"))
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(settings.OTP_MAX_ATTEMPTS):
        with pytest.raises(HTTPException) as err:
            run(otp.verify_otp("a@b.com", wrong))
        assert err.value.status_code == 401
    assert "otp:a@b.com:code" not in redis.data
    assert not run(otp.is_locked("a@b.com"))
    with pytest.raises(HTTPException) as err:
        run(otp.verify_otp("a@b.com", code))
    assert err.value.status_code == 401


def test_a_new_code_works_right_after_the_old_one_was_cancelled(redis):
    code = run(otp.generate_otp("a@b.com"))
    for _ in range(settings.OTP_MAX_ATTEMPTS):
        with pytest.raises(HTTPException):
            run(otp.verify_otp("a@b.com", "x" + code))
    fresh = run(otp.generate_otp("a@b.com"))
    assert run(otp.verify_otp("a@b.com", fresh)) is True


def test_send_limits_are_the_relaxed_ones(redis):
    for _ in range(settings.OTP_MAX_SENDS_PER_HOUR):
        redis.data.pop("otp:a@b.com:cooldown", None)
        run(otp.check_rate_limit("a@b.com"))
    redis.data.pop("otp:a@b.com:cooldown", None)
    with pytest.raises(HTTPException) as err:
        run(otp.check_rate_limit("a@b.com"))
    assert err.value.status_code == 429
    assert settings.OTP_MAX_SENDS_PER_HOUR >= 10 and settings.OTP_COOLDOWN_SECONDS <= 30
