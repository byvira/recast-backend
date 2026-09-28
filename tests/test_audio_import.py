"""Tests for importing audio from a link: the guarded fetcher, the podcast
feed reader, and the two endpoints. No real network: the HTTP layer is a
mock transport and DNS checks are faked, so these prove the guards, not
someone else's server.
"""
import httpx
import pytest

from app.pipelines.audio import link_fetch
from app.pipelines.audio.link_fetch import LinkFetchError, is_video_link, parse_feed
from app.pipelines.text.scraper import BlockedURLError
from tests.test_audio_assets import _h, _setup, _wav, stubs  # noqa: F401 — fixture reuse

_FEED = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>The Weekly Build</title>
    <item>
      <title>Episode 2</title>
      <pubDate>Tue, 02 Sep 2025 10:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/ep2.mp3" type="audio/mpeg" length="123"/>
      <itunes:duration>01:02:03</itunes:duration>
    </item>
    <item>
      <title>Show notes only</title>
      <enclosure url="https://cdn.example.com/notes.pdf" type="application/pdf" length="1"/>
    </item>
    <item><title>No enclosure</title></item>
    <item>
      <title>Episode 1</title>
      <enclosure url="https://cdn.example.com/ep1.m4a" type="" length="1"/>
      <itunes:duration>754</itunes:duration>
    </item>
  </channel>
</rss>"""


# ── feed reader ──────────────────────────────────────────────────────────────

def test_feed_lists_only_audio_episodes_with_real_details():
    feed = parse_feed(_FEED)
    assert feed.feed_title == "The Weekly Build"
    assert [e.title for e in feed.episodes] == ["Episode 2", "Episode 1"]
    assert feed.episodes[0].duration_s == 3723.0
    assert feed.episodes[0].published_at.year == 2025
    assert feed.episodes[1].duration_s == 754.0  # plain seconds


def test_feed_reader_refuses_entity_declarations_and_non_feeds():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
    with pytest.raises(LinkFetchError):
        parse_feed(bomb)
    with pytest.raises(LinkFetchError):
        parse_feed(b"<html><body>not a feed</body></html>")
    with pytest.raises(LinkFetchError):
        parse_feed(b"<rss><channel><title>Empty</title></channel></rss>")


def test_video_sites_are_recognised():
    assert is_video_link("https://www.youtube.com/watch?v=abc")
    assert is_video_link("https://youtu.be/abc")
    assert is_video_link("https://m.youtube.com/watch?v=abc")
    assert not is_video_link("https://cdn.example.com/ep.mp3")
    assert not is_video_link("https://notyoutube.com/ep.mp3")


# ── guarded fetcher ──────────────────────────────────────────────────────────

@pytest.fixture
def net(monkeypatch):
    """Serve fake responses and fake the DNS check: 127.0.0.1 and localhost
    count as private, everything else as public."""
    state = {"handler": None, "calls": []}

    def _guard(url):
        host = link_fetch.urlparse(url).hostname
        if host in ("127.0.0.1", "localhost", "10.0.0.5"):
            raise BlockedURLError("private")

    def _handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append(str(request.url))
        return state["handler"](request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(link_fetch, "_guard_public_url", _guard)
    monkeypatch.setattr(
        link_fetch.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw),
    )
    return state


async def test_a_public_audio_file_is_fetched(net):
    net["handler"] = lambda req: httpx.Response(200, content=_wav(0.2), headers={"content-type": "audio/x-wav"})
    body, mime, name = await link_fetch.fetch_audio("https://cdn.example.com/takes/My%20Take.wav", 5_000_000)
    assert mime == "audio/wav" and len(body) > 0 and name == "My Take.wav"


async def test_a_generic_content_type_falls_back_to_the_file_extension(net):
    net["handler"] = lambda req: httpx.Response(200, content=b"x" * 10, headers={"content-type": "application/octet-stream"})
    _, mime, _ = await link_fetch.fetch_audio("https://cdn.example.com/ep.mp3", 1000)
    assert mime == "audio/mpeg"


async def test_private_addresses_are_refused_up_front(net):
    for url in ("http://127.0.0.1/a.mp3", "http://localhost:8000/a.mp3", "http://10.0.0.5/a.mp3"):
        with pytest.raises(LinkFetchError, match="can't be reached"):
            await link_fetch.fetch_audio(url, 1000)
    assert net["calls"] == []  # nothing was even requested


async def test_a_redirect_into_a_private_address_is_refused(net):
    net["handler"] = lambda req: httpx.Response(302, headers={"location": "http://127.0.0.1/secret.mp3"})
    with pytest.raises(LinkFetchError, match="can't be reached"):
        await link_fetch.fetch_audio("https://cdn.example.com/ep.mp3", 1000)
    assert net["calls"] == ["https://cdn.example.com/ep.mp3"]


async def test_redirect_loops_stop(net):
    net["handler"] = lambda req: httpx.Response(302, headers={"location": "https://cdn.example.com/again.mp3"})
    with pytest.raises(LinkFetchError, match="too many"):
        await link_fetch.fetch_audio("https://cdn.example.com/ep.mp3", 1000)


async def test_size_is_capped_by_header_and_by_actual_bytes(net):
    net["handler"] = lambda req: httpx.Response(200, content=b"x" * 10, headers={"content-type": "audio/mpeg", "content-length": "999999999"})
    with pytest.raises(LinkFetchError, match="larger"):
        await link_fetch.fetch_audio("https://cdn.example.com/big.mp3", 1000)

    # No honest content-length: the body itself is counted.
    net["handler"] = lambda req: httpx.Response(200, content=b"x" * 5000, headers={"content-type": "audio/mpeg"})
    with pytest.raises(LinkFetchError, match="larger"):
        await link_fetch.fetch_audio("https://cdn.example.com/big.mp3", 1000)


async def test_non_audio_and_error_responses_are_refused(net):
    net["handler"] = lambda req: httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})
    with pytest.raises(LinkFetchError, match="isn't an audio file"):
        await link_fetch.fetch_audio("https://cdn.example.com/page", 1000)

    net["handler"] = lambda req: httpx.Response(404)
    with pytest.raises(LinkFetchError, match="error"):
        await link_fetch.fetch_audio("https://cdn.example.com/missing.mp3", 1000)


# ── endpoints ────────────────────────────────────────────────────────────────

async def test_import_creates_a_recording_like_an_upload(signup_user, stubs, monkeypatch):
    from app.api.v1 import audio_assets as audio_module

    client, _, ws_id, brand_id = await _setup(signup_user)
    original = _wav(1.0, noisy=True)

    async def _fake_fetch(url, max_bytes):
        assert url == "https://cdn.example.com/ep.wav"
        return original, "audio/wav", "ep.wav"

    monkeypatch.setattr(audio_module, "fetch_audio", _fake_fetch)

    res = await client.post(
        "/api/v1/audio-assets/import",
        json={"brand_id": brand_id, "url": "https://cdn.example.com/ep.wav", "title": "Guest episode"},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    asset = res.json()
    assert asset["title"] == "Guest episode" and asset["source_type"] == "uploaded"
    assert asset["dsp_settings"], "cleanup runs on imported audio too"
    assert [w["word"] for w in asset["transcript"]] == ["hello", "world"]
    assert asset["original_media_id"]  # so A/B works for it


async def test_import_reports_a_bad_link_as_a_400_with_a_reason(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    video = await client.post(
        "/api/v1/audio-assets/import",
        json={"brand_id": brand_id, "url": "https://www.youtube.com/watch?v=abc"}, headers=_h(ws_id),
    )
    assert video.status_code == 400 and "Raw Upload" in video.json()["detail"]

    private = await client.post(
        "/api/v1/audio-assets/import",
        json={"brand_id": brand_id, "url": "http://127.0.0.1:9/a.mp3"}, headers=_h(ws_id),
    )
    assert private.status_code == 400 and "can't be reached" in private.json()["detail"]

    junk = await client.post(
        "/api/v1/audio-assets/import", json={"brand_id": brand_id, "url": "ftp://x/y.mp3"}, headers=_h(ws_id),
    )
    assert junk.status_code == 400


async def test_feed_endpoint_lists_episodes(signup_user, stubs, monkeypatch):
    from app.api.v1 import audio_assets as audio_module

    client, _, ws_id, _ = await _setup(signup_user)

    async def _fake_list(url):
        return parse_feed(_FEED)

    monkeypatch.setattr(audio_module, "list_feed_episodes", _fake_list)
    res = await client.post("/api/v1/audio-assets/import/feed", json={"url": "https://example.com/feed.xml"}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["feed_title"] == "The Weekly Build"
    assert [e["title"] for e in res.json()["episodes"]] == ["Episode 2", "Episode 1"]

    async def _bad(url):
        raise LinkFetchError("That link isn't a podcast feed.")

    monkeypatch.setattr(audio_module, "list_feed_episodes", _bad)
    bad = await client.post("/api/v1/audio-assets/import/feed", json={"url": "https://example.com/x"}, headers=_h(ws_id))
    assert bad.status_code == 400
