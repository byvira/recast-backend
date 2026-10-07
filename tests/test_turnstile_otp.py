"""The browser security check on "send code"."""

import httpx
import pytest

from app.core import turnstile
from app.core.config import settings
from tests.conftest import unique_email


class _Reply:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _stand_in_cloudflare(monkeypatch, body=None, fail=False):
    calls = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            calls.append(data)
            if fail:
                raise httpx.ConnectError("down")
            return _Reply(body)

    monkeypatch.setattr(turnstile.httpx, "AsyncClient", Client)
    return calls


async def test_nothing_is_checked_when_no_secret_is_set(monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "")
    assert await turnstile.verify_turnstile("") is True


async def test_missing_token_fails_without_calling_cloudflare(monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "secret")
    calls = _stand_in_cloudflare(monkeypatch, {"success": True})
    assert await turnstile.verify_turnstile("  ") is False
    assert calls == []


async def test_a_passing_and_a_failing_token(monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "secret")
    _stand_in_cloudflare(monkeypatch, {"success": True})
    assert await turnstile.verify_turnstile("t", "1.2.3.4") is True
    _stand_in_cloudflare(monkeypatch, {"success": False})
    assert await turnstile.verify_turnstile("t") is False


async def test_unreachable_cloudflare_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "secret")
    _stand_in_cloudflare(monkeypatch, fail=True)
    assert await turnstile.verify_turnstile("t") is False


async def test_send_code_needs_the_check_once_a_secret_is_set(api_client, monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "secret")
    _stand_in_cloudflare(monkeypatch, {"success": True})
    email = unique_email()
    res = await api_client.post("/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"})
    assert res.status_code == 400
    assert "security check" in res.json()["detail"].lower()
    res = await api_client.post(
        "/api/v1/auth/request-otp", json={"identifier": email, "channel": "email", "turnstile_token": "good"}
    )
    assert res.status_code == 200
