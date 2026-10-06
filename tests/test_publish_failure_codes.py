"""What a publish failure is called, and the small platform fixes that go with it: a usage ceiling is never a sign-in problem, every
failure has a stable code the screens can act on, Threads tokens renew with Threads' own endpoint, and YouTube only offers the
visibility Google will actually keep. No network and no database."""
from datetime import datetime, timezone

import httpx
import pytest

from app.core.config import settings
from app.pipelines.publish.meta import oauth as meta_oauth
from app.pipelines.publish.meta import facebook, instagram, threads
from app.pipelines.publish.supervisor.classifier import (
    FAILURE_CODES, RETRYABLE_CODES, ErrorType, classify_error, failure_code,
)
from app.pipelines.publish.youtube.metadata import visibility_problem


def _code(status: int, message: str) -> str:
    return failure_code(classify_error(status, message), status, message)


def test_a_youtube_quota_403_is_not_a_reconnect():
    message = "The request cannot be completed because you have exceeded your quota."
    assert classify_error(403, message) == ErrorType.TRANSIENT
    assert _code(403, message) == "quota_exceeded"
    assert _code(403, "uploadLimitExceeded") == "quota_exceeded"


def test_a_real_permission_403_is_still_a_reconnect():
    assert classify_error(403, "Insufficient permissions for this action.") == ErrorType.AUTH
    assert _code(403, "Insufficient permissions for this action.") == "reconnect_required"
    assert _code(401, "Invalid token") == "reconnect_required"


@pytest.mark.parametrize("status,message,expected", [
    (429, "Too Many Requests", "rate_limited"),
    (500, "Internal error", "platform_unavailable"),
    (503, "service unavailable", "platform_unavailable"),
    (400, "Text too long for this platform", "content_invalid"),
    (400, "Invalid media: unsupported format", "media_invalid"),
    (400, "Account suspended", "policy_blocked"),
    (404, "Something unknown", "publish_failed"),
])
def test_each_failure_gets_a_stable_code(status, message, expected):
    assert _code(status, message) == expected


def test_every_code_is_listed_and_only_waiting_kinds_are_retryable():
    for status, message in ((403, "quota"), (429, "x"), (401, "x"), (500, "x"), (400, "too long"), (400, "image required"), (400, "spam"), (404, "x")):
        assert _code(status, message) in FAILURE_CODES
    assert RETRYABLE_CODES == {"rate_limited", "quota_exceeded", "platform_unavailable"}
    assert "reconnect_required" not in RETRYABLE_CODES


class _FakeClient:
    """Stands in for httpx.AsyncClient and records the request."""
    calls: list = []
    reply: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, **kwargs):
        _FakeClient.calls.append((url, params))
        return httpx.Response(200, json=_FakeClient.reply, request=httpx.Request("GET", url))


async def test_a_threads_token_is_renewed_with_the_threads_endpoint_and_no_app_secret(monkeypatch):
    _FakeClient.calls, _FakeClient.reply = [], {"access_token": "new-token", "expires_in": 5184000}
    monkeypatch.setattr(meta_oauth.httpx, "AsyncClient", _FakeClient)

    renewed = await threads.ThreadsPublisher().refresh_token("old-token")

    url, params = _FakeClient.calls[0]
    assert url == "https://graph.threads.net/refresh_access_token"
    assert params == {"grant_type": "th_refresh_token", "access_token": "old-token"}
    assert renewed["access_token"] == "new-token"
    assert renewed["expires_at"] > datetime.now(timezone.utc)


async def test_a_threads_renewal_error_is_reported_with_the_platforms_message(monkeypatch):
    _FakeClient.calls, _FakeClient.reply = [], {"error": {"message": "The token cannot be refreshed yet."}}
    monkeypatch.setattr(meta_oauth.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(ValueError, match="cannot be refreshed yet"):
        await meta_oauth.refresh_threads_token("old-token")


async def test_facebook_and_instagram_renewal_still_uses_the_facebook_exchange(monkeypatch):
    _FakeClient.calls, _FakeClient.reply = [], {"access_token": "page-token", "expires_in": 5184000}
    monkeypatch.setattr(meta_oauth.httpx, "AsyncClient", _FakeClient)

    await meta_oauth.refresh_meta_token("old-token")

    url, params = _FakeClient.calls[0]
    assert "graph.facebook.com" in url and params["grant_type"] == "fb_exchange_token"


def test_facebook_and_instagram_publish_on_the_same_graph_version_as_sign_in():
    assert facebook.GRAPH_BASE == meta_oauth.GRAPH_BASE == instagram.GRAPH_BASE
    assert "v19" not in facebook.GRAPH_BASE


def test_only_private_is_offered_until_google_has_passed_the_audit(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_API_AUDIT_PASSED", False)
    assert visibility_problem("private") is None
    assert "Private" in visibility_problem("public")
    assert visibility_problem("unlisted") is not None

    monkeypatch.setattr(settings, "YOUTUBE_API_AUDIT_PASSED", True)
    assert visibility_problem("public") is None
    assert visibility_problem("unlisted") is None
