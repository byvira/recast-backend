"""Reading a connected account's public profile (picture, display name, followers) from its platform, without calling any platform."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.pipelines.publish import profile


class _Client:
    """Answers each address with a prepared reply and records what was asked."""

    def __init__(self, replies: dict[str, tuple[int, dict]]):
        self.replies = replies
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for suffix, (status, body) in self.replies.items():
            if url.endswith(suffix) or suffix in url:
                return httpx.Response(status, json=body, request=httpx.Request("GET", url))
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))


TOKEN = {"access_token": "tok", "platform_user_id": "1789"}


async def test_instagram_gives_the_real_handle_picture_and_followers():
    client = _Client({"/1789": (200, {"username": "virastudio2026", "name": "Vira Studio", "profile_picture_url": "https://cdn.example/p.jpg", "followers_count": 1204})})
    found = await profile.fetch_profile("instagram", TOKEN, client)
    assert found == {"username": "virastudio2026", "display_name": "Vira Studio", "avatar_url": "https://cdn.example/p.jpg", "followers": 1204}
    assert client.calls[0][1]["params"]["access_token"] == "tok"


async def test_a_facebook_page_gives_its_name_picture_and_fans():
    client = _Client({"/1789": (200, {"name": "Vira Studio", "fan_count": 88, "picture": {"data": {"url": "https://cdn.example/f.jpg"}}})})
    assert await profile.fetch_profile("facebook", TOKEN, client) == {"display_name": "Vira Studio", "avatar_url": "https://cdn.example/f.jpg", "followers": 88}


async def test_bluesky_is_read_from_the_public_api_with_no_sign_in():
    client = _Client({"app.bsky.actor.getProfile": (200, {"handle": "vira.bsky.social", "displayName": "Vira", "avatar": "https://cdn.bsky.app/a.jpg", "followersCount": 12})})
    found = await profile.fetch_profile("bluesky", {"access_token": "tok", "platform_user_id": "did:plc:abc"}, client)
    assert found["username"] == "vira.bsky.social" and found["followers"] == 12
    url, kwargs = client.calls[0]
    assert url.startswith("https://public.api.bsky.app") and kwargs["params"] == {"actor": "did:plc:abc"} and "headers" not in kwargs


async def test_linkedin_gives_the_members_name_and_picture():
    client = _Client({"/userinfo": (200, {"name": "Sam Rivera", "picture": "https://media.licdn.com/p.jpg"})})
    assert await profile.fetch_profile("linkedin", TOKEN, client) == {"display_name": "Sam Rivera", "avatar_url": "https://media.licdn.com/p.jpg"}


async def test_a_youtube_channel_gives_its_short_address_unless_subscribers_are_hidden():
    body = {"items": [{"snippet": {"title": "Vira TV", "customUrl": "@viratv", "thumbnails": {"default": {"url": "https://yt3.example/d.jpg"}, "medium": {"url": "https://yt3.example/m.jpg"}}}, "statistics": {"subscriberCount": "540"}}]}
    found = await profile.fetch_profile("youtube", TOKEN, _Client({"/channels": (200, body)}))
    assert found == {"display_name": "Vira TV", "avatar_url": "https://yt3.example/m.jpg", "followers": 540, "profile_url": "https://www.youtube.com/@viratv"}
    body["items"][0]["statistics"]["hiddenSubscriberCount"] = True
    assert "followers" not in await profile.fetch_profile("youtube", TOKEN, _Client({"/channels": (200, body)}))


async def test_only_https_pictures_are_kept_and_an_unreachable_platform_gives_nothing():
    unsafe = _Client({"/1789": (200, {"name": "Vira", "picture": {"data": {"url": "http://cdn.example/f.jpg"}}})})
    assert await profile.fetch_profile("facebook", TOKEN, unsafe) == {"display_name": "Vira"}
    assert await profile.fetch_profile("instagram", TOKEN, _Client({"/1789": (400, {})})) == {}
    assert await profile.fetch_profile("reddit", TOKEN, _Client({})) == {}


def test_who_needs_a_refresh():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert profile.needs_refresh({"platform": "instagram"}, now) is True
    assert profile.needs_refresh({"platform": "instagram", "avatar_url": "https://x/p.jpg"}, now) is False
    assert profile.needs_refresh({"platform": "twitter"}, now) is False
    assert profile.needs_refresh({"platform": "instagram", "profile_checked_at": now - timedelta(hours=2)}, now) is False
    assert profile.needs_refresh({"platform": "instagram", "profile_checked_at": now - timedelta(hours=30)}, now) is True
    assert profile.needs_refresh({"platform": "instagram", "profile_checked_at": (now - timedelta(hours=2)).replace(tzinfo=None).isoformat()}, now) is False


async def test_a_refresh_saves_what_was_found_and_always_notes_when_it_looked(monkeypatch):
    saved = {}

    class _Collection:
        async def update_one(self, flt, update):
            saved["filter"], saved["update"] = flt, update

    async def token(workspace_id, platform):
        return TOKEN

    async def found(platform, tok, client):
        return {"display_name": "Vira", "avatar_url": "https://cdn.example/p.jpg", "followers": 5, "junk": "ignored"}

    monkeypatch.setattr(profile, "workspace_connections", _Collection())
    monkeypatch.setattr(profile, "get_token", token)
    monkeypatch.setattr(profile, "fetch_profile", found)

    assert await profile.refresh_profile("w1", "instagram") is True
    fields = saved["update"]["$set"]
    assert fields["display_name"] == "Vira" and fields["avatar_url"] == "https://cdn.example/p.jpg" and "junk" not in fields and "profile_checked_at" in fields
    assert saved["filter"] == {"workspace_id": "w1", "platform": "instagram"}


async def test_a_failed_refresh_still_notes_the_check_so_it_is_not_retried_all_day(monkeypatch):
    saved = {}

    class _Collection:
        async def update_one(self, flt, update):
            saved["update"] = update

    async def token(workspace_id, platform):
        return TOKEN

    async def boom(platform, tok, client):
        raise RuntimeError("down")

    monkeypatch.setattr(profile, "workspace_connections", _Collection())
    monkeypatch.setattr(profile, "get_token", token)
    monkeypatch.setattr(profile, "fetch_profile", boom)
    assert await profile.refresh_profile("w1", "instagram") is False
    assert list(saved["update"]["$set"]) == ["profile_checked_at"]


def test_background_reads_are_off_in_tests():
    from app.core.config import settings

    assert settings.PROFILE_REFRESH_ENABLED is False
