"""Meta connections renew with their access token before they expire, and the performance hint stays off until it is switched on."""
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.pipelines.text import performance_hint
from app.workers import token_refresh


class _Publisher:
    def __init__(self):
        self.received: list[str] = []

    async def refresh_token(self, value: str) -> dict:
        self.received.append(value)
        return {"access_token": "new-access", "expires_at": datetime.now(timezone.utc) + timedelta(days=60)}


def _patch_renewal(monkeypatch):
    publisher = _Publisher()
    saved: list[dict] = []

    async def _save(**kwargs):
        saved.append(kwargs)

    monkeypatch.setattr(token_refresh, "get_publisher", lambda platform: publisher)
    monkeypatch.setattr(token_refresh, "decrypt_token", lambda value: f"plain:{value}")
    monkeypatch.setattr(token_refresh, "save_token", _save)
    return publisher, saved


def _account(platform: str, **extra) -> dict:
    return {"workspace_id": "w1", "platform": platform, "access_token": "enc-access", "refresh_token": None,
            "platform_user_id": "u1", "username": "name", "connected_by": "m1", **extra}


async def test_a_meta_connection_with_no_refresh_token_is_renewed_with_its_access_token(monkeypatch):
    publisher, saved = _patch_renewal(monkeypatch)
    ok, error = await token_refresh.refresh_connection(_account("instagram"))

    assert ok and error == ""
    assert publisher.received == ["plain:enc-access"]
    assert saved[0]["access_token"] == "new-access"
    assert saved[0]["refresh_token"] is None
    assert saved[0]["recovered_via"] == "automatic renewal"


async def test_a_platform_with_a_refresh_token_is_renewed_with_that_token(monkeypatch):
    publisher, saved = _patch_renewal(monkeypatch)
    ok, _ = await token_refresh.refresh_connection(_account("linkedin", refresh_token="enc-refresh"))

    assert ok
    assert publisher.received == ["plain:enc-refresh"]
    assert saved[0]["refresh_token"] == "plain:enc-refresh"


async def test_a_non_meta_platform_with_no_refresh_token_asks_for_a_reconnect(monkeypatch):
    publisher, saved = _patch_renewal(monkeypatch)
    ok, error = await token_refresh.refresh_connection(_account("linkedin"))

    assert not ok and "manual reconnect" in error
    assert publisher.received == [] and saved == []


async def test_a_failed_renewal_is_reported_not_raised(monkeypatch):
    publisher, _ = _patch_renewal(monkeypatch)

    async def _boom(value):
        raise RuntimeError("Meta said no")

    publisher.refresh_token = _boom
    ok, error = await token_refresh.refresh_connection(_account("threads"))
    assert not ok and error == "Meta said no"


def test_the_performance_hint_is_off_by_default():
    assert settings.PERFORMANCE_HINT_IN_PROMPTS is False


async def test_the_hint_says_nothing_while_it_is_off(monkeypatch):
    monkeypatch.setattr(settings, "PERFORMANCE_HINT_IN_PROMPTS", False)
    assert await performance_hint.best_post_hint("w1", "LinkedIn") is None


def test_the_opening_of_a_post_is_its_first_line_trimmed():
    text = "\n\n  A strong opening line   with  spaces  \nSecond line"
    assert performance_hint._opening(text) == "A strong opening line with spaces"
    assert len(performance_hint._opening("x" * 300)) == 110
