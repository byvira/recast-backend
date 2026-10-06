"""The rest of the platform fields: LinkedIn several pictures, picture descriptions and link cards (the newer Posts API), Instagram tagged
people and collaborators, a Threads carousel, Facebook picture description, a YouTube playlist, a Bluesky link card, and the playlist
and place lookups. Platform calls are replaced by stand-ins that record what was sent."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.models.media import MediaAsset
from app.pipelines.publish import options as publish_options
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.bluesky import publisher as bluesky_module
from app.pipelines.publish.generic.safe_url import UnsafeUrl
from app.pipelines.publish.linkedin import posts_api
from app.pipelines.publish.linkedin import publisher as linkedin_module
from app.pipelines.publish.link_preview import LinkPreview, fetch_preview, parse_preview
from app.pipelines.publish.meta import facebook as facebook_module
from app.pipelines.publish.meta import instagram as instagram_module
from app.pipelines.publish.meta import threads as threads_module
from app.pipelines.publish.token_store import save_token
from app.pipelines.publish.youtube import publisher as youtube_module
from tests.conftest import create_workspace
from tests.test_attachments import H


def _image(media_id: str, alt: str | None = None) -> MediaAsset:
    return MediaAsset(
        id=media_id, workspace_id="w1", kind="image", url=f"https://res.cloudinary.com/demo/image/upload/{media_id}.jpg",
        mime_type="image/jpeg", source="uploaded", created_by="u1", created_at=datetime.now(timezone.utc), alt_text=alt,
    )


def _request(platform: str, media=(), content: str = "A post", **options) -> PublishRequest:
    return PublishRequest(
        piece_id="p1", user_id="u1", brand_id="b1", platform=platform, content=content, workspace_id="w1",
        platform_user_id="user-1", media=list(media), options=options,
    )


class _Recorder:
    calls: list = []
    post_headers: dict = {}
    status = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _reply(self, method, url, status=None):
        body = {"id": "obj-1", "uri": "at://did/x/1", "cid": "c", "permalink": "https://x.example/p",
                "value": {"uploadUrl": "https://upload.example/1", "image": "urn:li:image:1"}}
        return httpx.Response(status or _Recorder.status, json=body, headers=_Recorder.post_headers, request=httpx.Request(method, url))

    async def post(self, url, **kwargs):
        _Recorder.calls.append(("post", url, kwargs))
        return self._reply("POST", url)

    async def put(self, url, **kwargs):
        _Recorder.calls.append(("put", url, kwargs))
        return httpx.Response(201, request=httpx.Request("PUT", url))

    async def get(self, url, **kwargs):
        _Recorder.calls.append(("get", url, kwargs))
        return httpx.Response(200, content=b"image-bytes", headers={"content-type": "image/jpeg"}, request=httpx.Request("GET", url))


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.calls, _Recorder.post_headers, _Recorder.status = [], {}, 200
    for module in (posts_api, facebook_module, instagram_module, threads_module, youtube_module, bluesky_module, linkedin_module):
        monkeypatch.setattr(module.httpx, "AsyncClient", _Recorder)
    return _Recorder


# ── Settings ──────────────────────────────────────────────────────────

def test_the_new_settings_are_accepted_and_cleaned():
    assert publish_options.clean_options("instagram", {"user_tags": ["@a.b", "c", "A.B"], "collaborators": ["x_y"]}) == {"user_tags": ["a.b", "c"], "collaborators": ["x_y"]}
    assert publish_options.clean_options("youtube", {"playlist_id": " PL123 "}) == {"playlist_id": "PL123"}
    assert publish_options.clean_options("linkedin", {"link_card_url": "https://example.com/a"}) == {"link_card_url": "https://example.com/a"}
    assert publish_options.clean_options("bluesky", {"link_card_url": "https://example.com/a"}) == {"link_card_url": "https://example.com/a"}


@pytest.mark.parametrize("platform,raw,fragment", [
    ("instagram", {"collaborators": ["a", "b", "c", "d"]}, "up to 3"),
    ("instagram", {"user_tags": ["bad name!"]}, "usernames"),
    ("linkedin", {"link_card_url": "http://example.com"}, "https://"),
    ("bluesky", {"link_card_url": "javascript:alert(1)"}, "https://"),
    ("facebook", {"playlist_id": "x"}, "isn't available"),
])
def test_a_bad_new_setting_is_refused_in_plain_words(platform, raw, fragment):
    with pytest.raises(ValueError, match=fragment):
        publish_options.clean_options(platform, raw)


# ── Link previews ─────────────────────────────────────────────────────

def test_a_page_gives_its_card_details_from_its_own_tags():
    page = '<html><head><title>Plain</title><meta property="og:title" content="Five ways &amp; more"><meta name="description" content="Fallback">' \
           '<meta property="og:description" content="Small changes"><meta property="og:image" content="/cover.png"></head></html>'
    preview = parse_preview(page, "https://example.com/post")
    assert (preview.title, preview.description) == ("Five ways & more", "Small changes")
    assert preview.image_url == "https://example.com/cover.png"


def test_a_page_with_no_tags_falls_back_to_its_title():
    preview = parse_preview("<html><head><title> My   page </title></head></html>", "https://example.com/")
    assert (preview.title, preview.description, preview.image_url) == ("My page", "", None)


@pytest.mark.parametrize("url", ["http://example.com/a", "https://127.0.0.1/a", "https://localhost/a", "https://user:pw@example.com/a"])
async def test_an_address_that_may_not_be_read_is_refused_before_any_request(url):
    with pytest.raises(UnsafeUrl):
        await fetch_preview(url)


# ── LinkedIn newer API ────────────────────────────────────────────────

def test_commentary_is_escaped_but_hashtags_stay_hashtags():
    assert posts_api.escape_commentary("Ship (fast) #review @team *now* a_b ~c|d") == r"Ship \(fast\) #review \@team \*now\* a\_b \~c\|d"


def test_which_posts_need_the_newer_api():
    assert posts_api.needs_posts_api(_request("linkedin", [_image("a"), _image("b")])) is True
    assert posts_api.needs_posts_api(_request("linkedin", [_image("a", alt="A chart")])) is True
    assert posts_api.needs_posts_api(_request("linkedin", [_image("a")], link_card_url="https://example.com/x")) is True
    assert posts_api.needs_posts_api(_request("linkedin", [_image("a")])) is False
    assert posts_api.needs_posts_api(_request("linkedin")) is False


def test_the_post_content_is_one_picture_several_or_a_card():
    assert posts_api.build_content([("urn:1", "Alt")], None) == {"media": {"id": "urn:1", "altText": "Alt"}}
    assert posts_api.build_content([("urn:1", None), ("urn:2", "B")], None) == {"multiImage": {"images": [{"id": "urn:1"}, {"id": "urn:2", "altText": "B"}]}}
    assert posts_api.build_content([], {"source": "https://x"}) == {"article": {"source": "https://x"}}
    assert posts_api.build_content([], None) is None


async def test_several_pictures_are_uploaded_and_posted_with_their_descriptions(recorder):
    _Recorder.status = 201
    _Recorder.post_headers = {"x-restli-id": "urn:li:share:99"}
    request = _request("linkedin", [_image("a", "First"), _image("b")], content="Ship (fast)", visibility="CONNECTIONS")

    result = await linkedin_module.LinkedInPublisher().publish(request, "token")

    assert result.success and result.platform_post_id == "urn:li:share:99"
    posts = [c for c in _Recorder.calls if c[0] == "post" and c[1].endswith("/rest/posts")]
    payload = posts[0][2]["json"]
    assert payload["commentary"] == r"Ship \(fast\)" and payload["visibility"] == "CONNECTIONS"
    assert payload["content"]["multiImage"]["images"][0] == {"id": "urn:li:image:1", "altText": "First"}
    assert posts[0][2]["headers"]["Linkedin-Version"] == posts_api.LINKEDIN_VERSION
    assert len([c for c in _Recorder.calls if c[0] == "put"]) == 2


async def test_a_link_card_is_built_from_the_page(recorder, monkeypatch):
    _Recorder.status = 201
    _Recorder.post_headers = {"x-restli-id": "urn:li:share:7"}

    async def preview(url):
        return LinkPreview(url=url, title="The guide", description="How it works", image_url="https://example.com/c.png")

    monkeypatch.setattr("app.pipelines.publish.link_preview.fetch_preview", preview)
    result = await linkedin_module.LinkedInPublisher().publish(_request("linkedin", link_card_url="https://example.com/guide"), "token")

    assert result.success
    article = next(c for c in _Recorder.calls if c[1].endswith("/rest/posts"))[2]["json"]["content"]["article"]
    assert article["source"] == "https://example.com/guide" and article["title"] == "The guide" and article["thumbnail"] == "urn:li:image:1"


async def test_a_post_that_does_not_need_the_newer_api_keeps_the_old_path(recorder, monkeypatch):
    called = []

    async def never(request, token):
        called.append(1)

    monkeypatch.setattr(posts_api, "publish_with_posts_api", never)
    _Recorder.status = 201
    _Recorder.post_headers = {"x-restli-id": "urn:li:share:1"}
    await linkedin_module.LinkedInPublisher().publish(_request("linkedin"), "token")

    assert called == []
    assert any(c[1].endswith("/ugcPosts") for c in _Recorder.calls)


async def test_a_linkedin_error_is_classified_and_a_failed_picture_is_reported(recorder):
    _Recorder.status = 429
    result = await linkedin_module.LinkedInPublisher().publish(_request("linkedin", [_image("a"), _image("b")]), "token")
    assert not result.success and result.error_code == 429 and result.retry_after == 60


# ── Instagram, Threads, Facebook ──────────────────────────────────────

async def test_instagram_sends_tagged_people_and_collaborators(recorder, monkeypatch):
    async def ready(*args, **kwargs):
        return True, None

    monkeypatch.setattr(instagram_module, "_wait_until_ready", ready)
    request = _request("instagram", [_image("a")], user_tags=["ann", "bo"], collaborators=["partner"])
    result = await instagram_module.InstagramPublisher().publish(request, "token")

    assert result.success
    container = next(c for c in _Recorder.calls if c[1].endswith("/media"))[2]["params"]
    assert json.loads(container["collaborators"]) == ["partner"]
    tags = json.loads(container["user_tags"])
    assert [t["username"] for t in tags] == ["ann", "bo"] and all(0 < t["x"] < 1 and t["y"] == 0.5 for t in tags)


async def test_instagram_tags_the_first_picture_of_a_carousel_only(recorder, monkeypatch):
    async def ready(*args, **kwargs):
        return True, None

    monkeypatch.setattr(instagram_module, "_wait_until_ready", ready)
    request = _request("instagram", [_image("a"), _image("b")], user_tags=["ann"])
    await instagram_module.InstagramPublisher().publish(request, "token")

    children = [c[2]["params"] for c in _Recorder.calls if c[1].endswith("/media") and c[2]["params"].get("is_carousel_item")]
    assert "user_tags" in children[0] and "user_tags" not in children[1]


async def test_several_pictures_on_threads_become_a_carousel(recorder):
    request = _request("threads", [_image("a", "One"), _image("b")], reply_control="followers_only")
    result = await threads_module.ThreadsPublisher().publish(request, "token")

    assert result.success
    posts = [c[2]["params"] for c in _Recorder.calls if c[0] == "post" and c[1].endswith("/threads")]
    assert [p.get("is_carousel_item") for p in posts[:2]] == ["true", "true"] and posts[0]["alt_text"] == "One"
    parent = posts[2]
    assert parent["media_type"] == "CAROUSEL" and parent["children"] == "obj-1,obj-1" and parent["reply_control"] == "followers_only"
    assert "image_url" not in parent


async def test_a_facebook_picture_carries_its_description(recorder):
    await facebook_module.FacebookPublisher().publish(_request("facebook", [_image("a", "A team photo")]), "token")
    assert next(c for c in _Recorder.calls if c[1].endswith("/photos"))[2]["params"]["alt_text_custom"] == "A team photo"


# ── YouTube and Bluesky ───────────────────────────────────────────────

async def test_a_video_is_added_to_the_chosen_playlist_and_a_failure_is_only_a_note(recorder):
    publisher = youtube_module.YouTubePublisher()
    async with _Recorder() as client:
        ok = await publisher._add_to_playlist(client, "token", "vid-1", "PL1")
    body = next(c for c in _Recorder.calls if c[1] == youtube_module.YOUTUBE_PLAYLIST_ITEMS_URL)[2]["json"]
    assert ok is None and body["snippet"]["playlistId"] == "PL1" and body["snippet"]["resourceId"]["videoId"] == "vid-1"

    _Recorder.status = 403
    async with _Recorder() as client:
        note = await publisher._add_to_playlist(client, "token", "vid-1", "PL1")
    assert "playlist" in note


async def test_a_bluesky_link_card_has_the_pages_title_and_a_thumbnail(monkeypatch):
    class _Bsky(_Recorder):
        async def post(self, url, **kwargs):
            return httpx.Response(200, json={"blob": {"ref": "blob-1"}}, request=httpx.Request("POST", url))

    async def preview(url):
        return LinkPreview(url=url, title="The guide", description="How it works", image_url="https://example.com/c.png")

    monkeypatch.setattr("app.pipelines.publish.link_preview.fetch_preview", preview)
    monkeypatch.setattr(bluesky_module.httpx, "AsyncClient", _Bsky)
    monkeypatch.setattr(bluesky_module, "fit_image_for_bluesky", lambda data, mime: (data, "image/jpeg"))

    embed = await bluesky_module.BlueSkyPublisher()._link_card("https://example.com/guide", "token")

    assert embed["$type"] == "app.bsky.embed.external"
    assert embed["external"] == {"uri": "https://example.com/guide", "title": "The guide", "description": "How it works", "thumb": {"ref": "blob-1"}}


# ── The lookups ───────────────────────────────────────────────────────

async def test_the_lookups_need_the_account_connected_and_a_real_search(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Lookup WS")

    playlists = await client.get("/api/v1/publish/youtube/playlists", headers=H(ws_id))
    places = await client.get("/api/v1/publish/instagram/locations", params={"q": "Chennai"}, headers=H(ws_id))
    short = await client.get("/api/v1/publish/instagram/locations", params={"q": "C"}, headers=H(ws_id))

    assert playlists.status_code == 400 and "not connected" in playlists.json()["detail"]
    assert places.status_code == 400
    assert short.status_code == 200 and short.json() == {"items": [], "note": None}


async def test_a_connected_account_lists_its_playlists_and_places(signup_user, monkeypatch):
    from app.api.v1 import publish as publish_module

    class _Lookup(_Recorder):
        async def get(self, url, **kwargs):
            if "playlists" in url:
                body = {"items": [{"id": "PL1", "snippet": {"title": "Tips"}}, {"snippet": {"title": "No id"}}]}
            else:
                body = {"data": [{"id": "77", "name": "Chennai Cafe", "location": {"city": "Chennai", "country": "India"}}]}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Lookup WS 2")
    for platform in ("youtube", "instagram"):
        await save_token(workspace_id=ws_id, platform=platform, access_token="t", refresh_token=None, expires_at=None, platform_user_id="acct", username="u", connected_by="")
    monkeypatch.setattr("httpx.AsyncClient", _Lookup)

    playlists = (await client.get("/api/v1/publish/youtube/playlists", headers=H(ws_id))).json()
    places = (await client.get("/api/v1/publish/instagram/locations", params={"q": "Chennai"}, headers=H(ws_id))).json()

    assert playlists == {"items": [{"id": "PL1", "title": "Tips"}]}
    assert places["items"] == [{"id": "77", "name": "Chennai Cafe", "detail": "Chennai, India"}]
    assert publish_module  # the routes live in the publish router


# ── Bluesky video ─────────────────────────────────────────────────────

class _VideoServer:
    """Answers the calls of Bluesky's video flow and remembers them."""

    def __init__(self, states):
        self.calls = []
        self._states = list(states)

    def client(self):
        server = self

        class _Client:
            async def get(self, url, **kwargs):
                server.calls.append(("get", url, kwargs))
                request = httpx.Request("GET", url)
                if "plc.directory" in url:
                    return httpx.Response(200, json={"service": [{"id": "#atproto_pds", "serviceEndpoint": "https://morel.host.bsky.network"}]}, request=request)
                if url.endswith("getServiceAuth"):
                    return httpx.Response(200, json={"token": "service-token"}, request=request)
                return httpx.Response(200, json={"jobStatus": server._states.pop(0)}, request=request)

            async def post(self, url, **kwargs):
                server.calls.append(("post", url, kwargs))
                return httpx.Response(200, json={"jobId": "job-1", "state": "JOB_STATE_CREATED"}, request=httpx.Request("POST", url))

        return _Client()


async def _no_wait(_seconds):
    return None


async def test_a_bluesky_video_is_uploaded_through_the_video_service_and_waited_for():
    from app.pipelines.publish.bluesky import video

    server = _VideoServer([{"state": "JOB_STATE_RUNNING"}, {"state": "JOB_STATE_COMPLETED", "blob": {"ref": "video-blob"}}])
    blob = await video.upload_video(
        server.client(), pds_base="https://bsky.social/xrpc", access_token="token", did="did:plc:abc", video=b"video", mime_type="video/mp4", name="p.mp4", sleep=_no_wait,
    )

    assert blob == {"ref": "video-blob"}
    auth = next(c for c in server.calls if c[1].endswith("getServiceAuth"))[2]["params"]
    assert auth["aud"] == "did:web:morel.host.bsky.network" and auth["lxm"] == "com.atproto.repo.uploadBlob"
    upload = next(c for c in server.calls if c[1].endswith("uploadVideo"))
    assert upload[2]["headers"]["Authorization"] == "Bearer service-token" and upload[2]["params"]["did"] == "did:plc:abc"


async def test_a_video_that_fails_processing_or_never_finishes_says_so():
    from app.pipelines.publish.bluesky import video

    failed = _VideoServer([{"state": "JOB_STATE_FAILED", "error": "Unsupported codec"}])
    with pytest.raises(video.VideoError, match="Unsupported codec"):
        await video.upload_video(failed.client(), pds_base="x", access_token="t", did="did:plc:abc", video=b"v", mime_type="video/mp4", name="p.mp4", sleep=_no_wait)

    slow = _VideoServer([{"state": "JOB_STATE_RUNNING"}] * (video.MAX_POLLS + 1))
    with pytest.raises(video.VideoError, match="still processing"):
        await video.upload_video(slow.client(), pds_base="x", access_token="t", did="did:plc:abc", video=b"v", mime_type="video/mp4", name="p.mp4", sleep=_no_wait)


async def test_an_account_that_is_not_did_plc_and_a_too_large_video_are_refused_plainly():
    from app.pipelines.publish.bluesky import video

    server = _VideoServer([])
    with pytest.raises(video.VideoError, match="kind of Bluesky account"):
        await video.upload_video(server.client(), pds_base="x", access_token="t", did="did:web:example.com", video=b"v", mime_type="video/mp4", name="p.mp4")
    with pytest.raises(video.VideoError, match="300 MB"):
        await video.upload_video(server.client(), pds_base="x", access_token="t", did="did:plc:abc", video=b"x" * (video.MAX_BYTES + 1), mime_type="video/mp4", name="p.mp4")


async def test_a_bluesky_post_with_a_video_names_it_in_the_post_and_a_failure_leaves_a_text_post(monkeypatch):
    from app.pipelines.publish.bluesky import video

    posted = []

    class _Bsky(_Recorder):
        async def post(self, url, **kwargs):
            posted.append(kwargs.get("json"))
            return httpx.Response(200, json={"uri": "at://did/app.bsky.feed.post/abc", "cid": "c"}, request=httpx.Request("POST", url))

    async def fine(client, **kwargs):
        return {"ref": "video-blob"}

    async def broken(client, **kwargs):
        raise video.VideoError("Bluesky takes videos up to 300 MB.")

    clip = MediaAsset(id="v1", workspace_id="w1", kind="video", url="https://cdn.example/v.mp4", mime_type="video/mp4", source="uploaded", created_by="u", created_at=datetime.now(timezone.utc))
    monkeypatch.setattr(bluesky_module.httpx, "AsyncClient", _Bsky)

    monkeypatch.setattr(video, "upload_video", fine)
    ok = await bluesky_module.BlueSkyPublisher().publish(_request("bluesky", [clip], "Watch this"), "token")
    assert ok.success and posted[-1]["record"]["embed"] == {"$type": "app.bsky.embed.video", "video": {"ref": "video-blob"}}

    monkeypatch.setattr(video, "upload_video", broken)
    text_only = await bluesky_module.BlueSkyPublisher().publish(_request("bluesky", [clip], "Watch this"), "token")
    assert text_only.success and "embed" not in posted[-1]["record"] and "without the video" in text_only.media_dropped_reason
