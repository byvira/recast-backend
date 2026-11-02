"""A YouTube link is written from the words spoken in it (its captions), and falls back to the page text when there are none.
No real YouTube call is made here: the caption reader is replaced by a stand-in."""
from __future__ import annotations

from app.pipelines.text import scraper, youtube_captions

SPOKEN = " ".join(["We moved our whole team to one weekly planning call and it changed how we ship."] * 6)


def test_every_common_youtube_link_gives_the_video_id_and_nothing_else_does():
    vid = "dQw4w9WgXcQ"
    for url in (f"https://www.youtube.com/watch?v={vid}&t=10", f"https://youtu.be/{vid}", f"https://youtube.com/shorts/{vid}",
                f"https://www.youtube.com/embed/{vid}", f"https://m.youtube.com/live/{vid}?x=1"):
        assert youtube_captions.video_id(url) == vid, url
    for url in ("https://vimeo.com/123", "https://youtube.com/watch?v=bad", f"https://evil.example/watch?v={vid}", "not a link"):
        assert youtube_captions.video_id(url) is None, url


async def test_a_youtube_link_is_read_from_its_captions(monkeypatch):
    monkeypatch.setattr(youtube_captions, "captions_text", lambda vid: SPOKEN)

    def no_page(url):
        raise AssertionError("the page itself is not read when captions exist")

    monkeypatch.setattr(scraper, "_fetch_and_extract", no_page)
    monkeypatch.setattr(scraper, "_fetch_and_preview", no_page)
    assert await scraper.scrape_url("https://youtu.be/dQw4w9WgXcQ") == SPOKEN
    preview = await scraper.preview_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert preview["readable"] is True and preview["word_count"] == len(SPOKEN.split()) and preview["snippet"].startswith("We moved")


async def test_without_captions_a_youtube_link_falls_back_to_the_page_text(monkeypatch):
    monkeypatch.setattr(youtube_captions, "captions_text", lambda vid: None)
    monkeypatch.setattr(scraper, "_fetch_and_extract", lambda url: "A description of the video. " * 10)
    assert (await scraper.scrape_url("https://youtu.be/dQw4w9WgXcQ")).startswith("A description of the video.")


async def test_any_caption_failure_is_quietly_none(monkeypatch):
    import youtube_transcript_api

    class Broken:
        def list(self, vid):
            raise RuntimeError("YouTube refused this server")

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", Broken)
    assert youtube_captions.captions_text("dQw4w9WgXcQ") is None
