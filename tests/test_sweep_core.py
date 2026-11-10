"""Faults found by the core sweep: each test failed before its fix."""
import asyncio

from starlette.requests import Request

from app.core import sessions
from app.db.mongo import users, workspace_members, workspaces
from tests.conftest import create_workspace


async def test_many_requests_at_once_for_a_person_with_no_workspace_make_one_workspace(signup_user):
    client, profile = await signup_user("Fresh")
    ws = await create_workspace(client, "Temporary")
    await workspace_members.delete_many({"user_id": profile["id"]})
    await workspaces.delete_many({"owner_id": profile["id"]})
    await users.update_one({"id": profile["id"]}, {"$unset": {"default_workspace_id": ""}})
    assert ws

    answers = await asyncio.gather(*[client.get("/api/v1/content-guard/review-requests/none") for _ in range(8)])
    assert all(a.status_code == 200 for a in answers), [a.text for a in answers]

    made = await workspaces.count_documents({"owner_id": profile["id"]})
    member_of = await workspace_members.count_documents({"user_id": profile["id"], "status": "active"})
    assert made == 1 and member_of == 1, f"{made} workspaces and {member_of} memberships were made"
    me = await users.find_one({"id": profile["id"]})
    assert me["default_workspace_id"]


def _request(forwarded: str = "", host: str = "10.0.0.9") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (host, 1234), "method": "GET", "path": "/"})


def test_a_session_records_the_address_the_proxy_saw_not_one_the_caller_chose():
    assert sessions.client_ip(_request("1.2.3.4, 5.6.7.8")) == "5.6.7.8"
    assert sessions.client_ip(_request("5.6.7.8")) == "5.6.7.8"
    assert sessions.client_ip(_request("")) == "10.0.0.9"
    assert sessions.client_ip(_request(" , ")) == "10.0.0.9"


# ---- sign-in codes under a burst of requests ------------------------------------------------------------------------------------------
async def test_a_code_whose_guesses_are_used_up_is_dead_even_if_the_right_one_arrives_late():
    import pytest
    from fastapi import HTTPException

    from app.core import otp
    from app.core.config import settings
    from app.db.redis import get_redis

    who = "burst-guess@example.com"
    redis = await get_redis()
    await redis.delete(f"otp:{who}:code", f"otp:{who}:attempts")
    code = await otp.generate_otp(who)
    await redis.set(f"otp:{who}:attempts", str(settings.OTP_MAX_ATTEMPTS), ex=60)  # the state a burst of guesses leaves behind
    with pytest.raises(HTTPException) as err:
        await otp.verify_otp(who, code)
    assert err.value.status_code == 401
    assert not await redis.exists(f"otp:{who}:verified")
    await otp.clear_otp_state(who)


async def test_the_last_allowed_guess_can_still_be_the_right_one():
    from fastapi import HTTPException

    from app.core import otp
    from app.core.config import settings
    from app.db.redis import get_redis

    who = "last-guess@example.com"
    redis = await get_redis()
    await redis.delete(f"otp:{who}:code", f"otp:{who}:attempts")
    code = await otp.generate_otp(who)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(settings.OTP_MAX_ATTEMPTS - 1):
        try:
            await otp.verify_otp(who, wrong)
        except HTTPException:
            pass
    assert await otp.verify_otp(who, code) is True
    await otp.clear_otp_state(who)


async def test_a_burst_of_send_requests_sends_one_code_not_all_of_them():
    from fastapi import HTTPException

    from app.core import otp
    from app.db.redis import get_redis

    who = "burst-send@example.com"
    redis = await get_redis()
    await redis.delete(*[f"otp:{who}:{k}" for k in ("cooldown", "hour_count", "day_count")])

    async def one():
        try:
            await otp.check_rate_limit(who)
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    results = await asyncio.gather(*[one() for _ in range(10)])
    assert results.count(True) == 1, f"{results.count(True)} of 10 simultaneous requests were let through"
    assert int(await redis.get(f"otp:{who}:hour_count")) == 1, "the refused ones are not counted"
    await redis.delete(*[f"otp:{who}:{k}" for k in ("cooldown", "hour_count", "day_count")])


async def test_hitting_the_hourly_limit_does_not_keep_extending_it():
    from fastapi import HTTPException

    from app.core import otp
    from app.core.config import settings
    from app.db.redis import get_redis

    who = "hour-limit@example.com"
    redis = await get_redis()
    keys = [f"otp:{who}:{k}" for k in ("cooldown", "hour_count", "day_count")]
    await redis.delete(*keys)
    await redis.set(f"otp:{who}:hour_count", str(settings.OTP_MAX_SENDS_PER_HOUR), ex=1200)
    for _ in range(3):
        try:
            await otp.check_rate_limit(who)
            raised = False
        except HTTPException as exc:
            raised = "Hourly" in exc.detail
        assert raised
    assert int(await redis.get(f"otp:{who}:hour_count")) == settings.OTP_MAX_SENDS_PER_HOUR, "refused requests are not counted"
    assert 0 < await redis.ttl(f"otp:{who}:hour_count") <= 1200, "and they do not restart the hour"
    assert not await redis.exists(f"otp:{who}:cooldown"), "a refused request starts no cooldown"
    await redis.delete(*keys)


# ---- reading another workspace through a header --------------------------------------------------------------------------------------
async def test_the_profile_only_lists_brands_of_a_workspace_the_caller_belongs_to(signup_user):
    from uuid import uuid4

    from app.db.mongo import brand_profiles

    owner, owner_profile = await signup_user("Owner")
    outsider, _ = await signup_user("Outsider")
    ws = await create_workspace(owner, f"Private {uuid4().hex[:6]}")
    brand_id = f"b-{uuid4().hex}"
    await brand_profiles.insert_one({"id": brand_id, "workspace_id": ws, "user_id": owner_profile["id"]})
    try:
        mine = (await owner.get("/api/v1/users/me", headers={"X-Workspace-Id": ws})).json()
        assert brand_id in mine["brand_profiles"]
        seen = (await outsider.get("/api/v1/users/me", headers={"X-Workspace-Id": ws})).json()
        assert brand_id not in seen["brand_profiles"], "another workspace's brand ids were shown to someone outside it"
    finally:
        await brand_profiles.delete_many({"id": brand_id})


# ---- locks only let go of what they hold ---------------------------------------------------------------------------------------
async def test_a_job_that_ran_past_its_lock_time_does_not_delete_the_lock_another_instance_now_holds(monkeypatch):
    from app.core import scheduler_lock

    store: dict = {}

    class Redis:
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in store:
                return None
            store[key] = value
            return True

        async def get(self, key):
            return store.get(key)

        async def delete(self, key):
            store.pop(key, None)

    async def fake():
        return Redis()

    monkeypatch.setattr(scheduler_lock, "get_redis", fake)

    @scheduler_lock.distributed_job_lock("sweep-test", 5)
    async def slow_job():
        store["lock:scheduler:sweep-test"] = "another-instance"  # this lock ran out and another instance took it

    await slow_job()
    assert store.get("lock:scheduler:sweep-test") == "another-instance", "the first job removed a lock that was no longer its own"

    @scheduler_lock.distributed_job_lock("sweep-test-2", 5)
    async def quick_job():
        pass

    await quick_job()
    assert "lock:scheduler:sweep-test-2" not in store, "a job still lets go of its own lock"
