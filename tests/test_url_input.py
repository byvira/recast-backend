"""The URL input of the text pipeline: every way a link can fail is said in plain words, the preview and the run agree on what is
readable, a pasted site name is fixed, and History keeps the link. Network calls are mocked."""
from unittest.mock import patch

import httpx
import pytest

from app.pipelines.text import scraper
from app.pipelines.text.scraper import UrlFetchError, preview_url, scrape_url, tidy_address

ARTICLE = "<html><body><article>" + ("This is a sentence in a real article about family money. " * 30) + "</article></body></html>"


def _serve(monkeypatch, status=200, content_type="text/html; charset=utf-8", body=ARTICLE, raises=None):
    """Makes every download answer as given, without touching the network or the address check."""
    real_client = httpx.Client

    def handler(request):
        if raises:
            raise raises
        return httpx.Response(status, headers={"content-type": content_type}, content=body.encode())

    monkeypatch.setattr(scraper, "_guard_public_url", lambda url: None)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def test_a_pasted_site_name_is_given_https_and_spaces_are_trimmed():
    assert tidy_address("  example.com/article  ") == "https://example.com/article"
    assert tidy_address("www.example.com") == "https://www.example.com"
    assert tidy_address("https://example.com") == "https://example.com"
    assert tidy_address("not a link") == "not a link"
    assert tidy_address("") == ""


@pytest.mark.parametrize("status,expected", [
    (401, scraper.NEEDS_LOGIN), (403, scraper.NEEDS_LOGIN), (404, scraper.NOT_FOUND), (410, scraper.NOT_FOUND),
    (429, scraper.SITE_BUSY), (503, scraper.SITE_BUSY),
])
async def test_each_kind_of_refusal_has_its_own_plain_reason(monkeypatch, status, expected):
    _serve(monkeypatch, status=status, body="")
    result = await preview_url("https://example.com/post")
    assert result["readable"] is False and result["problem"] == expected and result["title"] is None
    with pytest.raises(UrlFetchError) as raised:
        await scrape_url("https://example.com/post")
    assert str(raised.value) == expected


@pytest.mark.parametrize("content_type,expected", [
    ("application/pdf", scraper.IS_A_PDF),
    ("image/png", scraper.NOT_AN_ARTICLE.format(kind="picture")),
    ("video/mp4", scraper.NOT_AN_ARTICLE.format(kind="video")),
    ("audio/mpeg", scraper.NOT_AN_ARTICLE.format(kind="recording")),
    ("application/octet-stream", scraper.NOT_A_PAGE),
])
async def test_a_link_to_something_that_is_not_an_article_says_what_it_is(monkeypatch, content_type, expected):
    _serve(monkeypatch, content_type=content_type, body="binary")
    assert (await preview_url("https://example.com/file"))["problem"] == expected
    with pytest.raises(UrlFetchError, match="."):
        await scrape_url("https://example.com/file")


async def test_a_slow_or_unreachable_site_is_told_so(monkeypatch):
    _serve(monkeypatch, raises=httpx.ReadTimeout("slow"))
    assert (await preview_url("https://example.com"))["problem"] == scraper.TOO_SLOW
    _serve(monkeypatch, raises=httpx.ConnectError("down"))
    assert (await preview_url("https://example.com"))["problem"] == scraper.CANT_OPEN


async def test_an_internal_address_is_refused_without_naming_it(monkeypatch):
    # the real address check, not the stand-in
    result = await preview_url("http://localhost:6379/")
    assert result["readable"] is False and result["problem"] == scraper.CANT_OPEN
    assert "localhost" not in result["problem"] and "6379" not in result["problem"]


async def test_something_that_is_not_a_web_address_is_refused_with_a_hint():
    with pytest.raises(ValueError, match="https://"):
        await preview_url("hello there")
    with pytest.raises(UrlFetchError, match="https://"):
        await scrape_url("ftp://example.com/file")


async def test_a_readable_article_previews_and_the_run_can_use_it(monkeypatch):
    _serve(monkeypatch)
    result = await preview_url("example.com/post")  # no https: it is added
    assert result["readable"] is True and result["problem"] is None and result["word_count"] > 100 and result["will_trim"] is False
    text = await scrape_url("example.com/post")
    assert "family money" in text


async def test_a_page_with_only_a_title_is_not_called_readable(monkeypatch):
    _serve(monkeypatch, body="<html><head><title>A Title</title></head><body><p>hi</p></body></html>")
    result = await preview_url("https://example.com")
    assert result["readable"] is False and result["problem"] == scraper.NO_TEXT
    with pytest.raises(UrlFetchError) as raised:
        await scrape_url("https://example.com")
    assert str(raised.value) == scraper.NO_TEXT, "the preview and the run must say the same thing"


async def test_a_very_long_page_says_only_its_start_is_used(monkeypatch):
    long_body = "<html><body><article>" + ("A long sentence that keeps going on and on. " * 400) + "</article></body></html>"
    _serve(monkeypatch, body=long_body)
    assert (await preview_url("https://example.com"))["will_trim"] is True


async def test_a_run_from_a_link_keeps_the_link_for_history():
    from app.models.text import InputSourceType, Platform
    from app.pipelines.text import normalizer

    async def fake_scrape(url):
        assert url == "https://example.com/post", "the tidied address is what is fetched"
        return "word " * 60

    with patch.object(normalizer, "scrape_url", fake_scrape):
        normalised = await normalizer.normalise_input(
            InputSourceType.URL, "  example.com/post ", [Platform.LINKEDIN], "u1", "b1", workspace_id="w1",
        )
    assert normalised.source_url == "https://example.com/post"
    assert normalised.raw_content.startswith("word")
