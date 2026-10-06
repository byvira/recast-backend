from app.api.v1.oauth import _derive_profile_url as derive, normalize_profile_url as norm


def test_platform_forms():
    assert derive("instagram", "acme") == "https://www.instagram.com/acme"
    assert derive("threads", "acme") == "https://www.threads.com/@acme"
    assert derive("bluesky", "acme.bsky.social") == "https://bsky.app/profile/acme.bsky.social"
    assert derive("youtube", "", "UC_abc-123") == "https://www.youtube.com/channel/UC_abc-123"
    assert derive("facebook", "My Page", "12345") == "https://www.facebook.com/12345"
    assert derive("reddit", "acme") == "https://www.reddit.com/user/acme"
    assert derive("twitter", "acme") == "https://x.com/acme"


def test_username_cleaning():
    assert derive("instagram", "  @acme ") == "https://www.instagram.com/acme"
    assert derive("instagram", "") is None
    assert derive("instagram", "@") is None
    assert derive("instagram", "a b") is None
    assert derive("instagram", "a/b") is None
    assert derive("threads", "acme?x=1") is None
    assert derive("youtube", "", "") is None


def test_no_public_address():
    assert derive("linkedin", "acme") is None
    assert derive("unknown", "acme") is None


def test_normalize_old_forms():
    assert norm("https://www.threads.net/@acme") == "https://www.threads.com/@acme"
    assert norm("https://threads.net/@acme") == "https://www.threads.com/@acme"
    assert norm("https://instagram.com/acme") == "https://www.instagram.com/acme"
    assert norm("https://facebook.com/123") == "https://www.facebook.com/123"
    assert norm("https://reddit.com/user/acme") == "https://www.reddit.com/user/acme"
    assert norm("https://bsky.app/profile/a.b") == "https://bsky.app/profile/a.b"


def test_normalize_rejects_non_http():
    assert norm(None) is None
    assert norm("") is None
    assert norm("javascript:alert(1)") is None
    assert norm("ftp://x.com/a") is None
    assert norm("acme") is None



def test_an_address_saved_from_a_display_name_with_spaces_is_never_shown():
    assert norm("https://www.instagram.com/Recast_other%20System%20User") is None
    assert norm("https://www.instagram.com/Recast_other System User") is None
    assert norm("https://www.instagram.com/virastudio2026") == "https://www.instagram.com/virastudio2026"


async def test_an_old_instagram_connection_is_repaired_with_the_real_handle(monkeypatch):
    from app.api.v1 import oauth
    from app.pipelines.publish.meta import oauth as meta_oauth

    saved = {}

    class _Collection:
        async def update_one(self, flt, update):
            saved["filter"], saved["update"] = flt, update

    async def handle(ig_user_id, access_token):
        assert (ig_user_id, access_token) == ("1789", "tok")
        return "virastudio2026"

    async def token(workspace_id, platform):
        return {"access_token": "tok"}

    import app.db.mongo as mongo
    import app.pipelines.publish.token_store as token_store

    monkeypatch.setattr(mongo, "workspace_connections", _Collection())
    monkeypatch.setattr(token_store, "get_token", token)
    monkeypatch.setattr(meta_oauth, "fetch_instagram_username", handle)

    account = {"platform": "instagram", "platform_user_id": "1789", "username": "Recast_other System User", "profile_url": None}
    await oauth._heal_instagram("w1", account)

    assert account["username"] == "virastudio2026" and account["profile_url"] == "https://www.instagram.com/virastudio2026"
    assert saved["update"]["$set"] == {"username": "virastudio2026", "profile_url": "https://www.instagram.com/virastudio2026"}


async def test_a_failed_repair_leaves_the_list_as_it_was(monkeypatch):
    from app.api.v1 import oauth
    from app.pipelines.publish.meta import oauth as meta_oauth

    async def nothing(ig_user_id, access_token):
        return None

    async def token(workspace_id, platform):
        return {"access_token": "tok"}

    import app.pipelines.publish.token_store as token_store

    monkeypatch.setattr(token_store, "get_token", token)
    monkeypatch.setattr(meta_oauth, "fetch_instagram_username", nothing)
    account = {"platform": "instagram", "platform_user_id": "1789", "username": "Name With Spaces", "profile_url": None}
    await oauth._heal_instagram("w1", account)
    assert account["profile_url"] is None and account["username"] == "Name With Spaces"
