"""The settings a post can carry beyond its text (a Facebook link, LinkedIn visibility, Threads reply control, YouTube language, Instagram
first comment, Bluesky languages), the Blog and Newsletter details, and the clickable links and hashtags on Bluesky. Platform calls are
replaced by a stand-in that records what was sent."""
import httpx
import pytest

from app.db.mongo import content_pieces
from app.pipelines.publish import options as publish_options
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.bluesky import publisher as bluesky_module
from app.pipelines.publish.bluesky.facets import build_facets, language_tags
from app.pipelines.publish.linkedin import publisher as linkedin_module
from app.pipelines.publish.meta import facebook as facebook_module
from app.pipelines.publish.meta import threads as threads_module
from tests.conftest import create_workspace
from tests.test_attachments import H, _piece


# ── Settings ──────────────────────────────────────────────────────────

def test_each_platform_accepts_only_its_own_settings():
    assert publish_options.clean_options("linkedin", {"visibility": "CONNECTIONS"}) == {"visibility": "CONNECTIONS"}
    assert publish_options.clean_options("facebook", {"link": "https://example.com/post"}) == {"link": "https://example.com/post"}
    assert publish_options.clean_options("threads", {"reply_control": "followers_only", "topic_tag": "Engineering"}) == {
        "reply_control": "followers_only", "topic_tag": "Engineering",
    }
    assert publish_options.clean_options("instagram", {"first_comment": "  #tips  ", "location_id": "12345"}) == {"first_comment": "#tips", "location_id": "12345"}
    assert publish_options.clean_options("youtube", {"language": "en-US", "notify_subscribers": False, "thumbnail_media_id": "m1", "captions": False}) == {
        "language": "en-US", "notify_subscribers": False, "thumbnail_media_id": "m1", "captions": False,
    }
    assert publish_options.clean_options("bluesky", {"languages": ["en", "ta"]}) == {"languages": ["en", "ta"]}


@pytest.mark.parametrize("platform,raw,fragment", [
    ("linkedin", {"visibility": "EVERYONE"}, "Choose one of"),
    ("linkedin", {"link": "https://x.example"}, "isn't available"),
    ("facebook", {"link": "http://example.com"}, "https://"),
    ("threads", {"topic_tag": "a.b"}, "period"),
    ("threads", {"reply_control": "nobody"}, "Choose one of"),
    ("instagram", {"location_id": "abc"}, "number"),
    ("instagram", {"first_comment": "x" * 2201}, "2200"),
    ("youtube", {"language": "english please"}, "language code"),
    ("youtube", {"notify_subscribers": "yes"}, "yes or no"),
    ("bluesky", {"languages": ["en", "fr", "de", "es"]}, "up to 3"),
    ("x", {"anything": 1}, "isn't available"),
])
def test_a_setting_that_is_not_allowed_is_refused_in_plain_words(platform, raw, fragment):
    with pytest.raises(ValueError, match=fragment):
        publish_options.clean_options(platform, raw)


def test_empty_values_clear_a_setting():
    assert publish_options.clean_options("facebook", {"link": ""}) == {}
    assert publish_options.clean_options("bluesky", {"languages": []}) == {}
    assert publish_options.clean_options("linkedin", None) == {}


def test_blog_and_newsletter_details_are_checked():
    cleaned = publish_options.clean_seo({"title": "  Five ways  ", "meta_description": "A summary", "tags": ["a", "b"], "hashtags": None})
    assert cleaned == {"title": "Five ways", "meta_description": "A summary", "tags": ["a", "b"], "hashtags": None}
    for bad, fragment in (({"title": "x" * 201}, "200"), ({"tags": ["ok", ""]}, "plain words"), ({"nope": "x"}, "can't be saved"), ({"tags": ["x"] * 21}, "up to 20")):
        with pytest.raises(ValueError, match=fragment):
            publish_options.clean_seo(bad)


# ── Bluesky links and hashtags ────────────────────────────────────────

def _slice(text: str, facet: dict) -> str:
    raw = text.encode("utf-8")
    return raw[facet["index"]["byteStart"]:facet["index"]["byteEnd"]].decode("utf-8")


def test_links_and_hashtags_become_facets_with_byte_positions():
    text = "Read northwind.example/reviews and https://recast.app/x. #codereview rocks"
    facets = build_facets(text)

    marked = [(_slice(text, f), f["features"][0]) for f in facets]
    assert marked[0][0] == "northwind.example/reviews" and marked[0][1]["uri"] == "https://northwind.example/reviews"
    assert marked[1][0] == "https://recast.app/x" and marked[1][1]["uri"] == "https://recast.app/x"
    assert marked[2][0] == "#codereview" and marked[2][1] == {"$type": "app.bsky.richtext.facet#tag", "tag": "codereview"}


def test_positions_count_bytes_not_characters():
    text = "Hello 😀 see https://example.com now"
    facet = build_facets(text)[0]
    assert _slice(text, facet) == "https://example.com"
    assert facet["index"]["byteStart"] == len("Hello 😀 see ".encode("utf-8"))


def test_ordinary_text_is_left_alone():
    assert build_facets("Done, i.e. finished. See you at 5.30 pm, e.g. tomorrow.") == []
    assert build_facets("Issue #1 and C# are fine") == []
    assert build_facets("") == []


def test_sentence_punctuation_is_not_part_of_the_link_and_overlaps_never_happen():
    text = "(see https://example.com/page). Also https://example.com/a#section!"
    uris = [f["features"][0]["uri"] for f in build_facets(text)]
    assert uris == ["https://example.com/page", "https://example.com/a#section"]


def test_language_tags_are_capped_at_three():
    assert language_tags({"languages": ["en", "ta", "hi", "fr"]}) == ["en", "ta", "hi"]
    assert language_tags({}) == []
    assert language_tags(None) == []


# ── What each platform receives ───────────────────────────────────────

class _Recorder:
    """Stands in for httpx.AsyncClient: answers every call with a success and remembers what was sent."""
    calls: list = []
    status = 200
    headers: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _reply(self, method, url):
        request = httpx.Request(method, url)
        return httpx.Response(_Recorder.status, json={"id": "post-1", "uri": "at://did/app.bsky.feed.post/abc", "cid": "c", "permalink": "https://x.example/p"}, headers=_Recorder.headers, request=request)

    async def post(self, url, **kwargs):
        _Recorder.calls.append(("post", url, kwargs))
        return self._reply("POST", url)

    async def get(self, url, **kwargs):
        _Recorder.calls.append(("get", url, kwargs))
        return self._reply("GET", url)


def _request(platform: str, content: str = "A short post", **options) -> PublishRequest:
    return PublishRequest(
        piece_id="p1", user_id="u1", brand_id="b1", platform=platform, content=content, workspace_id="w1",
        platform_user_id="user-1", options=options,
    )


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.calls, _Recorder.status, _Recorder.headers = [], 200, {}
    for module in (facebook_module, threads_module, bluesky_module, linkedin_module):
        monkeypatch.setattr(module.httpx, "AsyncClient", _Recorder)
    return _Recorder


async def test_a_facebook_text_post_carries_its_link(recorder):
    result = await facebook_module.FacebookPublisher().publish(_request("facebook", link="https://example.com/read"), "token")

    assert result.success
    _, url, kwargs = recorder.calls[0]
    assert url.endswith("/feed") and kwargs["params"]["link"] == "https://example.com/read"


async def test_a_facebook_post_with_no_link_sends_none(recorder):
    await facebook_module.FacebookPublisher().publish(_request("facebook"), "token")
    assert "link" not in recorder.calls[0][2]["params"]


async def test_a_threads_post_carries_reply_control_and_topic_tag(recorder):
    result = await threads_module.ThreadsPublisher().publish(_request("threads", reply_control="followers_only", topic_tag="Engineering"), "token")

    assert result.success
    params = recorder.calls[0][2]["params"]
    assert params["reply_control"] == "followers_only" and params["topic_tag"] == "Engineering" and params["media_type"] == "TEXT"


async def test_a_bluesky_post_marks_its_links_hashtags_and_languages(recorder):
    text = "Five changes. northwind.example/reviews #codereview"
    result = await bluesky_module.BlueSkyPublisher().publish(_request("bluesky", text, languages=["en", "ta"]), "token")

    assert result.success
    record = recorder.calls[0][2]["json"]["record"]
    assert record["langs"] == ["en", "ta"]
    assert [f["features"][0]["$type"].rsplit("#", 1)[1] for f in record["facets"]] == ["link", "tag"]


async def test_a_bluesky_post_with_nothing_to_mark_has_no_facets(recorder):
    await bluesky_module.BlueSkyPublisher().publish(_request("bluesky", "Plain words only"), "token")
    record = recorder.calls[0][2]["json"]["record"]
    assert "facets" not in record and "langs" not in record


async def test_a_linkedin_post_uses_the_chosen_visibility_and_defaults_to_public(recorder):
    recorder.status, recorder.headers = 201, {"x-restli-id": "urn:li:share:1"}
    await linkedin_module.LinkedInPublisher().publish(_request("linkedin", visibility="CONNECTIONS"), "token")
    await linkedin_module.LinkedInPublisher().publish(_request("linkedin"), "token")

    sent = [c[2]["json"]["visibility"]["com.linkedin.ugc.MemberNetworkVisibility"] for c in recorder.calls]
    assert sent == ["CONNECTIONS", "PUBLIC"]


# ── Saving them on a post ─────────────────────────────────────────────

async def test_settings_and_blog_details_are_saved_on_the_post(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Options WS 1")
    piece_id = await _piece(ws_id, profile["id"], platform="LinkedIn")

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}", headers=H(ws_id),
        json={"publish_options": {"visibility": "CONNECTIONS"}, "seo": {"title": "Five ways", "tags": ["code review"]}},
    )

    assert res.status_code == 200, res.text
    stored = await content_pieces.find_one({"piece_id": piece_id})
    assert stored["publish_options"] == {"visibility": "CONNECTIONS"}
    assert stored["seo"]["title"] == "Five ways" and stored["seo"]["tags"] == ["code review"]
    assert stored["content"] == "A real post."  # the text was not touched


async def test_a_setting_the_platform_does_not_have_is_refused_and_nothing_is_saved(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Options WS 2")
    piece_id = await _piece(ws_id, profile["id"], platform="LinkedIn")

    res = await client.patch(f"/api/v1/content/pieces/{piece_id}", json={"publish_options": {"reply_control": "everyone"}}, headers=H(ws_id))

    assert res.status_code == 422 and "isn't available" in res.json()["detail"]
    assert "publish_options" not in await content_pieces.find_one({"piece_id": piece_id})


async def test_the_text_can_still_be_edited_on_its_own_and_an_empty_request_is_refused(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Options WS 3")
    piece_id = await _piece(ws_id, profile["id"])

    edited = await client.patch(f"/api/v1/content/pieces/{piece_id}", json={"content": "A better post."}, headers=H(ws_id))
    empty = await client.patch(f"/api/v1/content/pieces/{piece_id}", json={}, headers=H(ws_id))
    blank = await client.patch(f"/api/v1/content/pieces/{piece_id}", json={"content": "   "}, headers=H(ws_id))

    assert edited.status_code == 200 and edited.json()["content"] == "A better post."
    assert empty.status_code == 400
    assert blank.status_code == 400


async def test_a_published_post_keeps_its_settings(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Options WS 4")
    piece_id = await _piece(ws_id, profile["id"], platform="LinkedIn")
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_status": "published"}})

    res = await client.patch(f"/api/v1/content/pieces/{piece_id}", json={"publish_options": {"visibility": "PUBLIC"}}, headers=H(ws_id))

    assert res.status_code == 409


# ── Instagram and YouTube ─────────────────────────────────────────────

def _image(media_id: str = "m1", alt_text: str | None = None):
    from datetime import datetime, timezone

    from app.models.media import MediaAsset

    return MediaAsset(
        id=media_id, workspace_id="w1", kind="image", url=f"https://res.cloudinary.com/demo/image/upload/{media_id}.png",
        mime_type="image/png", source="uploaded", created_by="u1", created_at=datetime.now(timezone.utc), alt_text=alt_text,
    )


async def test_an_instagram_post_sends_alt_text_and_location_and_adds_the_first_comment(monkeypatch):
    from app.pipelines.publish.meta import instagram as instagram_module

    async def ready(*args, **kwargs):
        return True, None

    _Recorder.calls, _Recorder.status, _Recorder.headers = [], 200, {}
    monkeypatch.setattr(instagram_module.httpx, "AsyncClient", _Recorder)
    monkeypatch.setattr(instagram_module, "_wait_until_ready", ready)
    request = _request("instagram", "Caption", first_comment="More tips #review", location_id="12345")
    request.media = [_image(alt_text="A chart of review time")]
    request.ig_user_id = "ig-1"

    result = await instagram_module.InstagramPublisher().publish(request, "token")

    assert result.success and result.media_dropped_reason is None
    container = next(c for c in _Recorder.calls if c[1].endswith("/media"))[2]["params"]
    assert container["alt_text"] == "A chart of review time" and container["location_id"] == "12345"
    comment = next(c for c in _Recorder.calls if c[1].endswith("/post-1/comments"))[2]["params"]
    assert comment["message"] == "More tips #review"


async def test_when_the_first_comment_fails_the_post_stands_and_the_member_is_told(monkeypatch):
    from app.pipelines.publish.meta import instagram as instagram_module

    class _CommentFails(_Recorder):
        async def post(self, url, **kwargs):
            _Recorder.calls.append(("post", url, kwargs))
            status = 400 if url.endswith("/comments") else 200
            return httpx.Response(status, json={"id": "post-1", "error": {"message": "no"}}, request=httpx.Request("POST", url))

    async def ready(*args, **kwargs):
        return True, None

    _Recorder.calls = []
    monkeypatch.setattr(instagram_module.httpx, "AsyncClient", _CommentFails)
    monkeypatch.setattr(instagram_module, "_wait_until_ready", ready)
    request = _request("instagram", "Caption", first_comment="Hello")
    request.media = [_image()]

    result = await instagram_module.InstagramPublisher().publish(request, "token")

    assert result.success is True
    assert "first comment couldn't be added" in result.media_dropped_reason


async def test_a_youtube_upload_carries_language_and_notify_and_the_thumbnail_is_set(monkeypatch):
    from datetime import datetime, timezone

    from app.db.mongo import media_assets
    from app.pipelines.publish.youtube import publisher as youtube_module
    from app.pipelines.publish.youtube.metadata import YouTubeMetadata

    class _Upload(_Recorder):
        async def put(self, url, **kwargs):
            _Recorder.calls.append(("put", url, kwargs))
            return httpx.Response(200, json={"id": "vid-1"}, request=httpx.Request("PUT", url))

    _Recorder.calls, _Recorder.status, _Recorder.headers = [], 200, {"Location": "https://upload.example/session"}
    publisher = youtube_module.YouTubePublisher()
    metadata = YouTubeMetadata(title="Five ways", description="Description")
    async with _Upload() as client:
        video_id = await publisher._upload_video(client, "token", b"video", "video/mp4", metadata, {"language": "ta", "notify_subscribers": False})

    assert video_id == "vid-1"
    init = _Recorder.calls[0]
    assert init[2]["params"]["notifySubscribers"] == "false"
    assert init[2]["json"]["snippet"]["defaultLanguage"] == "ta" and init[2]["json"]["snippet"]["defaultAudioLanguage"] == "ta"

    await media_assets.insert_one({
        "id": "thumb-1", "workspace_id": "w-thumb", "kind": "image", "url": "https://cdn.example/thumb.jpg", "mime_type": "image/jpeg",
        "source": "uploaded", "created_by": "u", "created_at": datetime.now(timezone.utc),
    })
    _Recorder.calls = []
    async with _Upload() as client:
        missing = await publisher._set_thumbnail(client, "token", "vid-1", "nope", "w-thumb")
        done = await publisher._set_thumbnail(client, "token", "vid-1", "thumb-1", "w-thumb")

    assert "couldn't be found" in missing
    assert done is None
    thumbnail_call = next(c for c in _Recorder.calls if c[1] == youtube_module.YOUTUBE_THUMBNAIL_URL)
    assert thumbnail_call[2]["params"]["videoId"] == "vid-1"
    assert thumbnail_call[2]["headers"]["Content-Type"] == "image/jpeg"


# ── The platform list tells screens what they need ────────────────────

async def test_the_platform_list_carries_the_picture_limits_and_whether_youtube_is_unlocked(signup_user, monkeypatch):
    from app.core.config import settings

    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Platform List WS")
    monkeypatch.setattr(settings, "YOUTUBE_API_AUDIT_PASSED", False)

    platforms = {p["key"]: p for p in (await client.get("/api/v1/platforms", headers=H(ws_id))).json()["platforms"]}

    assert platforms["instagram"]["max_images"] == 10 and platforms["bluesky"]["max_images"] == 4
    assert platforms["linkedin"]["max_images"] == 20 and platforms["threads"]["max_images"] == 20
    assert platforms["facebook"]["max_images"] == 10 and platforms["youtube"]["max_images"] is None
    assert platforms["youtube"]["publish_unlocked"] is False
    assert platforms["linkedin"]["publish_unlocked"] is None
