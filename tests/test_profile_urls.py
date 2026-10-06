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
