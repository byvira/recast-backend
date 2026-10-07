"""Sessions, revocation and refresh-token reuse, with Redis and the user lookup replaced by in-memory stand-ins."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core import auth


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def exists(self, key):
        return 1 if key in self.data else 0


class FakeUsers:
    def __init__(self, doc=None):
        self.doc = doc

    async def find_one(self, query, projection=None):
        return self.doc


@pytest.fixture
def stand_ins(monkeypatch):
    redis = FakeRedis()

    async def get_redis():
        return redis

    monkeypatch.setattr(auth, "get_redis", get_redis)
    people = FakeUsers({"id": "u1"})
    monkeypatch.setattr(auth, "users", people)
    return redis, people


def run(coro):
    return asyncio.run(coro)


def test_tokens_share_a_session_and_have_their_own_ids():
    access, refresh = auth.issue_tokens("u1")
    a = auth.verify_token(access, "access")
    r = auth.verify_token(refresh, "refresh")
    assert a["sid"] == r["sid"] and a["jti"] != r["jti"]
    assert auth.issue_tokens("u1", a["sid"])[0] != access


def test_blacklist_key_is_a_hash_not_the_token(stand_ins):
    redis, _ = stand_ins
    access, _ = auth.issue_tokens("u1")
    run(auth.blacklist_token(access))
    assert access not in "".join(redis.data)
    assert run(auth.is_token_blacklisted(access))


def test_old_unhashed_blacklist_entries_still_count(stand_ins):
    redis, _ = stand_ins
    redis.data["blacklist:legacy-token"] = "1"
    assert run(auth.is_token_blacklisted("legacy-token"))


def test_refresh_rotates_and_keeps_the_session(stand_ins):
    _, refresh = auth.issue_tokens("u1")
    sid = auth.session_id_of(refresh)
    new_access, new_refresh = run(auth.rotate_refresh_token(refresh))
    assert auth.session_id_of(new_access) == sid == auth.session_id_of(new_refresh)


def test_quick_second_use_is_refused_without_ending_the_session(stand_ins):
    _, refresh = auth.issue_tokens("u1")
    sid = auth.session_id_of(refresh)
    run(auth.rotate_refresh_token(refresh))
    with pytest.raises(HTTPException):
        run(auth.rotate_refresh_token(refresh))
    assert not run(auth.is_session_revoked(sid))


def test_late_reuse_ends_the_whole_session(stand_ins):
    redis, _ = stand_ins
    _, refresh = auth.issue_tokens("u1")
    sid = auth.session_id_of(refresh)
    new_access, new_refresh = run(auth.rotate_refresh_token(refresh))
    key = auth._blacklist_key(refresh)
    redis.data[key] = str((datetime.now(timezone.utc) - timedelta(seconds=60)).timestamp())
    with pytest.raises(HTTPException):
        run(auth.rotate_refresh_token(refresh))
    assert run(auth.is_session_revoked(sid))
    with pytest.raises(HTTPException):
        run(auth.rotate_refresh_token(new_refresh))


def test_revoked_session_blocks_the_access_token(stand_ins):
    from starlette.requests import Request

    access, _ = auth.issue_tokens("u1")
    sid = auth.session_id_of(access)
    request = Request({"type": "http", "headers": [(b"authorization", f"Bearer {access}".encode())]})
    assert run(auth.get_current_user(request))["id"] == "u1"
    run(auth.revoke_session(sid))
    with pytest.raises(HTTPException) as err:
        run(auth.get_current_user(request))
    assert err.value.status_code == 401


def test_sign_out_everywhere_rejects_older_tokens(stand_ins):
    _, people = stand_ins
    access, refresh = auth.issue_tokens("u1")
    people.doc = {"id": "u1", "tokens_valid_after": datetime.now(timezone.utc) + timedelta(seconds=5)}
    with pytest.raises(HTTPException):
        run(auth.rotate_refresh_token(refresh))
    payload = auth.verify_token(access, "access")
    assert auth._issued_before_cutoff(payload, people.doc)
    people.doc = {"id": "u1", "tokens_valid_after": datetime.now(timezone.utc) - timedelta(hours=1)}
    assert not auth._issued_before_cutoff(payload, people.doc)


def test_access_cookie_lasts_as_long_as_the_access_token():
    from fastapi import Response
    from app.core.config import settings

    response = Response()
    auth.set_auth_cookies(response, "a", "r")
    cookies = [h for h in response.raw_headers if h[0] == b"set-cookie"]
    access_cookie = next(v.decode() for _, v in cookies if v.startswith(b"access_token"))
    assert f"Max-Age={settings.JWT_EXPIRE_HOURS * 3600}" in access_cookie
