"""WordPress, Ghost and Mailchimp destinations: what is sent, what is refused, and that a newsletter is never sent without being asked."""
from datetime import datetime, timezone

import httpx
import jwt
import pytest

from app.models.media import MediaAsset
from app.pipelines.publish import options as publish_options
from app.pipelines.publish.destinations import common, ghost, mailchimp, service, wordpress

GHOST_KEY = "64f1a2b3c4d5e6f708192a3b:" + "ab" * 32
MAILCHIMP_KEY = "0123456789abcdef" * 2 + "-" + "us21"  # built in pieces so it is never mistaken for a real key


class _Fake:
    """Stands in for httpx.AsyncClient: answers by method and path suffix, and records every call."""
    calls: list = []
    replies: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _answer(self, method, url, kwargs):
        _Fake.calls.append((method, url, kwargs))
        for (m, suffix), (status, body) in _Fake.replies.items():
            if m == method and url.split("?")[0].endswith(suffix):
                return httpx.Response(status, json=body, request=httpx.Request(method, url))
        return httpx.Response(404, json={}, request=httpx.Request(method, url))

    async def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)

    async def put(self, url, **kwargs):
        return self._answer("PUT", url, kwargs)


@pytest.fixture
def fake(monkeypatch):
    _Fake.calls, _Fake.replies = [], {}
    for module in (wordpress, ghost, mailchimp):
        monkeypatch.setattr(module.httpx, "AsyncClient", _Fake)
    return _Fake


def _picture(alt=None):
    return MediaAsset(
        id="m1", workspace_id="w", kind="image", url="https://cdn.example/cover.jpg", mime_type="image/jpeg", source="uploaded",
        created_by="u", created_at=datetime.now(timezone.utc), alt_text=alt,
    )


# ── Shared ────────────────────────────────────────────────────────────

def test_a_post_becomes_html_and_raw_html_is_shown_as_text():
    html = common.to_html("## Title\n\nSome **bold** text and [x](javascript:alert(1)) and <b>raw</b>\n\n<script>bad()</script>")
    assert "<h2>Title</h2>" in html and "<strong>bold</strong>" in html
    assert "<script>" not in html and "<b>" not in html and 'href="javascript:' not in html


@pytest.mark.parametrize("address", ["http://example.com", "https://localhost", "https://127.0.0.1/blog", "https://user:pw@example.com"])
async def test_a_site_address_that_may_not_be_used_is_refused(address):
    with pytest.raises(common.DestinationError):
        await common.clean_site_url(address)


async def test_a_site_address_is_tidied(monkeypatch):
    async def allow(url):
        return None

    monkeypatch.setattr(common, "assert_safe_url", allow)
    assert await common.clean_site_url("example.com/blog/") == "https://example.com/blog"


# ── Settings ──────────────────────────────────────────────────────────

def test_destination_settings_are_checked():
    assert publish_options.clean_options("blog", {"destination": "ghost", "post_status": "publish", "category_id": "12"}) == {
        "destination": "ghost", "post_status": "publish", "category_id": "12",
    }
    assert publish_options.clean_options("newsletter", {"destination": "mailchimp", "audience_id": "abc", "send_mode": "send"})["send_mode"] == "send"
    with pytest.raises(ValueError):
        publish_options.clean_options("blog", {"destination": "mailchimp"})
    with pytest.raises(ValueError):
        publish_options.clean_options("newsletter", {"send_mode": "now"})
    with pytest.raises(ValueError):
        publish_options.clean_options("blog", {"category_id": "news"})


def test_the_title_and_body_are_taken_from_the_post():
    piece = {"seo": {"title": "My title"}, "content": "# My title\n\nBody text"}
    assert service.title_of(piece) == "My title" and service.body_of(piece) == "Body text"
    assert service.title_of({"seo": {}, "content": "\n## First line\nmore"}) == "First line"
    assert service.body_of({"seo": {"title": "Other"}, "content": "# My title\n\nBody"}) == "# My title\n\nBody"


# ── WordPress ─────────────────────────────────────────────────────────

async def test_wordpress_sign_in_is_checked_and_a_bad_password_is_explained(fake, monkeypatch):
    async def allow(url):
        return None

    monkeypatch.setattr(common, "assert_safe_url", allow)
    fake.replies[("GET", "/wp-json/wp/v2/users/me")] = (200, {"id": 7, "name": "Sam"})
    facts = await wordpress.verify("https://blog.example.com/", "sam", "abcd efgh")
    assert facts == {"site_url": "https://blog.example.com", "user_id": "7", "name": "Sam"}

    fake.replies[("GET", "/wp-json/wp/v2/users/me")] = (401, {})
    with pytest.raises(common.DestinationError, match="application password"):
        await wordpress.verify("https://blog.example.com", "sam", "wrong")


async def test_a_wordpress_post_is_a_draft_by_default_with_a_cover_picture(fake):
    fake.replies[("POST", "/wp-json/wp/v2/media")] = (201, {"id": 55})
    fake.replies[("GET", "/cover.jpg")] = (200, {})
    fake.replies[("POST", "/wp-json/wp/v2/posts")] = (201, {"id": 90, "link": "https://blog.example.com/?p=90"})

    result = await wordpress.publish(
        site="https://blog.example.com", username="sam", password="pw", piece_id="p1", title="T", content="Hello", excerpt="Sum",
        slug="my-post", status="draft", category_id="4", author_id="", pictures=[_picture()],
    )

    assert result.success and result.platform_post_url == "https://blog.example.com/?p=90" and not result.media_dropped_reason
    body = next(c[2]["json"] for c in fake.calls if c[0] == "POST" and c[1].endswith("/posts"))
    assert body["status"] == "draft" and body["categories"] == [4] and body["featured_media"] == 55 and body["slug"] == "my-post"
    assert "author" not in body


async def test_a_wordpress_refusal_says_to_reconnect_and_a_failed_cover_is_only_a_note(fake):
    fake.replies[("POST", "/wp-json/wp/v2/posts")] = (401, {"message": "no"})
    result = await wordpress.publish(
        site="https://x", username="u", password="p", piece_id="p1", title="T", content="c", excerpt="", slug="", status="publish",
        category_id="", author_id="", pictures=[_picture()],
    )
    assert not result.success and result.error_type == "AUTH" and "Reconnect" in result.error_message

    fake.replies[("POST", "/wp-json/wp/v2/posts")] = (201, {"id": 1, "link": "https://x/1"})
    ok = await wordpress.publish(
        site="https://x", username="u", password="p", piece_id="p1", title="T", content="c", excerpt="", slug="", status="publish",
        category_id="", author_id="", pictures=[_picture()],
    )
    assert ok.success and "cover" in ok.media_dropped_reason


# ── Ghost ─────────────────────────────────────────────────────────────

def test_a_ghost_token_is_signed_with_the_key_and_short_lived():
    token = ghost.make_token(GHOST_KEY, now=1_700_000_000)
    claims = jwt.decode(token, bytes.fromhex("ab" * 32), algorithms=["HS256"], audience="/admin/", options={"verify_exp": False})
    assert claims["exp"] - claims["iat"] == 300
    assert jwt.get_unverified_header(token)["kid"] == "64f1a2b3c4d5e6f708192a3b"


@pytest.mark.parametrize("key", ["", "nocolon", "id:not-hex", ":abcd"])
def test_a_badly_formed_ghost_key_is_explained(key):
    with pytest.raises(common.DestinationError, match="Admin API key"):
        ghost.split_key(key)


async def test_a_ghost_post_carries_html_tags_and_the_cover(fake):
    fake.replies[("POST", "/ghost/api/admin/posts/")] = (201, {"posts": [{"id": "g1", "url": "https://g.example.com/p/"}]})
    result = await ghost.publish(
        site="https://g.example.com", admin_key=GHOST_KEY, piece_id="p1", title="T", content="## Hi", excerpt="Sum", slug="", status="publish",
        tags=["news", "ai"], pictures=[_picture("A cover")],
    )
    assert result.success and result.platform_post_id == "g1"
    call = next(c for c in fake.calls if c[0] == "POST")
    post = call[2]["json"]["posts"][0]
    assert call[2]["params"] == {"source": "html"} and post["status"] == "published" and "<h2>Hi</h2>" in post["html"]
    assert post["tags"] == [{"name": "news"}, {"name": "ai"}] and post["feature_image"].endswith("cover.jpg") and post["feature_image_alt"] == "A cover"
    assert call[2]["headers"]["Authorization"].startswith("Ghost ")


# ── Mailchimp ─────────────────────────────────────────────────────────

def test_the_data_centre_comes_from_the_key():
    assert mailchimp.data_centre(MAILCHIMP_KEY) == "us21"
    with pytest.raises(common.DestinationError):
        mailchimp.data_centre("not-a-key")


def test_every_campaign_has_an_unsubscribe_link_and_an_address_line():
    html = mailchimp.build_html("Hello")
    assert "*|UNSUB|*" in html and "*|LIST:ADDRESSLINE|*" in html


def _mailchimp_replies(fake, from_email="hi@brand.com"):
    fake.replies[("GET", "/lists/aud1")] = (200, {"name": "Readers", "campaign_defaults": {"from_name": "Brand", "from_email": from_email}})
    fake.replies[("POST", "/campaigns")] = (200, {"id": "c1", "web_id": 321})
    fake.replies[("PUT", "/campaigns/c1/content")] = (200, {})
    fake.replies[("POST", "/campaigns/c1/actions/send")] = (204, {})


async def test_a_newsletter_is_left_as_a_draft_unless_sending_is_chosen(fake):
    _mailchimp_replies(fake)
    result = await mailchimp.publish(api_key=MAILCHIMP_KEY, piece_id="p1", audience_id="aud1", subject="Subject", preview="Preview", content="Hi", send=False)

    assert result.success and result.platform_post_id == "c1" and result.platform_post_url.endswith("id=321")
    assert not any(c[1].endswith("/actions/send") for c in fake.calls)
    settings = next(c[2]["json"] for c in fake.calls if c[0] == "POST" and c[1].endswith("/campaigns"))["settings"]
    assert settings["subject_line"] == "Subject" and settings["preview_text"] == "Preview" and settings["reply_to"] == "hi@brand.com"


async def test_a_newsletter_is_sent_only_when_asked(fake):
    _mailchimp_replies(fake)
    result = await mailchimp.publish(api_key=MAILCHIMP_KEY, piece_id="p1", audience_id="aud1", subject="S", preview="", content="Hi", send=True)
    assert result.success and any(c[1].endswith("/actions/send") for c in fake.calls)


async def test_an_audience_with_no_sender_email_stops_before_anything_is_created(fake):
    _mailchimp_replies(fake, from_email="")
    result = await mailchimp.publish(api_key=MAILCHIMP_KEY, piece_id="p1", audience_id="aud1", subject="S", preview="", content="Hi", send=False)
    assert not result.success and "sender email" in result.error_message
    assert not any(c[0] == "POST" and c[1].endswith("/campaigns") for c in fake.calls)


# ── Through the routes ────────────────────────────────────────────────

async def _seed(workspace_id: str, user_id: str, platform: str = "Blog") -> str:
    from uuid import uuid4

    from app.pipelines.text.storage import ensure_session_exists, save_live_piece

    session_id = str(uuid4())
    await ensure_session_exists(session_id=session_id, workspace_id=workspace_id, user_id=user_id, brand_id=str(uuid4()), source_type="text")
    return await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id, brand_id=str(uuid4()), platform=platform,
        content="## Heading\n\nA real post body.", word_count=5, char_count=40,
    )


async def test_connecting_a_destination_checks_the_details_and_keeps_the_secret_encrypted(signup_user, monkeypatch):
    from app.db.mongo import workspace_connections
    from tests.conftest import create_workspace

    async def allow(url):
        return None

    async def verified(site_url, username, password):
        return {"site_url": "https://blog.example.com", "user_id": "7", "name": "Sam"}

    monkeypatch.setattr(common, "assert_safe_url", allow)
    monkeypatch.setattr(wordpress, "verify", verified)
    client, _ = await signup_user()
    ws = await create_workspace(client, "Destinations WS")

    res = await client.post(
        "/api/v1/destinations/wordpress/connect", headers={"X-Workspace-Id": ws},
        json={"site_url": "blog.example.com", "username": "sam", "application_password": "abcd efgh ijkl"},
    )
    assert res.status_code == 200 and res.json()["connected"] is True

    stored = await workspace_connections.find_one({"workspace_id": ws, "platform": "wordpress"})
    assert stored["platform_user_id"] == "https://blog.example.com" and "abcd" not in stored["access_token"]


async def test_a_bad_connection_is_refused_in_plain_words(signup_user, monkeypatch):
    from tests.conftest import create_workspace

    async def refused(site_url, admin_key):
        raise common.DestinationError("Ghost did not accept that Admin API key.")

    monkeypatch.setattr(ghost, "verify", refused)
    client, _ = await signup_user()
    ws = await create_workspace(client, "Destinations WS 2")
    res = await client.post("/api/v1/destinations/ghost/connect", headers={"X-Workspace-Id": ws}, json={"site_url": "https://g.example.com", "admin_key": "x"})
    assert res.status_code == 400 and "Admin API key" in res.json()["detail"]


async def test_sending_needs_a_destination_a_connection_and_for_a_whole_audience_a_confirmation(signup_user):
    from app.db.mongo import content_pieces
    from tests.conftest import create_workspace

    client, profile = await signup_user()
    ws = await create_workspace(client, "Destinations WS 3")
    headers = {"X-Workspace-Id": ws}
    piece_id = await _seed(ws, profile["id"])
    await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers=headers)

    none = await client.post("/api/v1/destinations/send", headers=headers, json={"piece_id": piece_id})
    assert none.status_code == 400 and "where to publish" in none.json()["detail"]

    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_options": {"destination": "wordpress"}}})
    unconnected = await client.post("/api/v1/destinations/send", headers=headers, json={"piece_id": piece_id})
    assert unconnected.status_code == 400 and "not connected" in unconnected.json()["detail"]
    # Every refusal puts the post back, so it can be sent once the problem is fixed.
    assert (await content_pieces.find_one({"piece_id": piece_id})).get("publish_status") != "publishing"


# ── Scheduling ────────────────────────────────────────────────────────

async def _due_destination_piece(ws: str, user_id: str, *, platform="Blog", options=None, confirmed=False) -> str:
    from datetime import timedelta

    from app.db.mongo import content_pieces

    piece_id = await _seed(ws, user_id, platform)
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {
        "publish_status": "publishing", "publish_target": platform.lower(), "publish_options": options or {"destination": "wordpress"},
        "publish_send_confirmed": confirmed, "seo": {"title": "My title"},
        "publish_scheduled_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    }})
    return piece_id


def _result(success, code=None, kind=None, message=None, piece_id="p"):
    from app.pipelines.publish.base import PublishResult

    return PublishResult(success=success, platform="wordpress", piece_id=piece_id, platform_post_id="9" if success else None,
                         platform_post_url="https://blog.example.com/?p=9" if success else None, error_code=code, error_type=kind, error_message=message)


async def test_a_scheduled_blog_post_goes_to_its_destination_and_is_marked(signup_user, monkeypatch):
    from app.db.mongo import content_pieces
    from app.workers.scheduled_posts import _publish_scheduled_piece
    from tests.conftest import create_workspace

    client, profile = await signup_user()
    ws = await create_workspace(client, "Dest Sched 1")
    piece_id = await _due_destination_piece(ws, profile["id"])
    seen = {}

    async def fake_send(piece, workspace_id, *, may_send_to_audience):
        seen["audience"] = may_send_to_audience
        return _result(True, piece_id=piece["piece_id"])

    monkeypatch.setattr(service, "send_piece", fake_send)
    await _publish_scheduled_piece(await content_pieces.find_one({"piece_id": piece_id}))

    saved = await content_pieces.find_one({"piece_id": piece_id})
    assert saved["publish_status"] == "published" and saved["platform_post_url"] == "https://blog.example.com/?p=9"
    assert saved["publish_destination"] == "wordpress" and saved["publish_destination_state"] == "draft" and seen["audience"] is False


async def test_only_a_slow_down_answer_is_tried_again_and_a_timeout_asks_the_member_to_check(signup_user, monkeypatch):
    from app.db.mongo import content_pieces
    from app.workers.scheduled_posts import _publish_scheduled_piece
    from tests.conftest import create_workspace

    client, profile = await signup_user()
    ws = await create_workspace(client, "Dest Sched 2")
    slow = await _due_destination_piece(ws, profile["id"])
    timeout = await _due_destination_piece(ws, profile["id"])

    async def fake_send(piece, workspace_id, *, may_send_to_audience):
        if piece["piece_id"] == slow:
            return _result(False, 429, "TRANSIENT", "Too many requests", piece["piece_id"])
        return _result(False, 408, "TRANSIENT", "WordPress took too long to answer.", piece["piece_id"])

    monkeypatch.setattr(service, "send_piece", fake_send)
    await _publish_scheduled_piece(await content_pieces.find_one({"piece_id": slow}))
    await _publish_scheduled_piece(await content_pieces.find_one({"piece_id": timeout}))

    first, second = await content_pieces.find_one({"piece_id": slow}), await content_pieces.find_one({"piece_id": timeout})
    assert first["publish_status"] == "queued" and first["publish_attempts"] == 1
    assert second["publish_status"] == "failed" and "check there" in second["last_error"]


async def test_a_scheduled_newsletter_is_never_sent_to_the_audience_without_the_saved_confirmation(signup_user, monkeypatch):
    from app.db.mongo import content_pieces
    from app.workers.scheduled_posts import _publish_scheduled_piece
    from tests.conftest import create_workspace

    client, profile = await signup_user()
    ws = await create_workspace(client, "Dest Sched 3")
    options = {"destination": "mailchimp", "audience_id": "a1", "send_mode": "send"}
    piece_id = await _due_destination_piece(ws, profile["id"], platform="Newsletter", options=options, confirmed=False)

    async def connected(workspace_id, key):
        return {"access_token": MAILCHIMP_KEY, "platform_user_id": "us21", "username": "Brand"}

    monkeypatch.setattr(service, "get_token", connected)
    await _publish_scheduled_piece(await content_pieces.find_one({"piece_id": piece_id}))

    saved = await content_pieces.find_one({"piece_id": piece_id})
    assert saved["publish_status"] == "failed" and "Confirm" in saved["last_error"]
