"""app.pipelines.text.scraper.preview_url — backs POST /text/preview-url.

The URL input tab's "Fetch" button used to show a hardcoded fake preview
card for literally any URL typed in (title "How Community-Led Growth
Changed Our Trajectory", source "blog.example.com" — never real). This
replaced it with a real (mocked-network) scrape preview.

Network calls are mocked — same reason nothing else in this suite makes a
real HTTP request to third-party sites.
"""

from unittest.mock import patch

import pytest

from app.pipelines.text.scraper import BlockedURLError, preview_url


class _FakeMetadata:
    def __init__(self, title):
        self.title = title


async def test_preview_url_returns_title_snippet_and_word_count():
    fake_html = "<html>fake</html>"
    fake_text = "word " * 250  # 250 words, well past the 220-char snippet cap

    with patch("app.pipelines.text.scraper._guard_public_url"), \
         patch("app.pipelines.text.scraper._safe_download", return_value=fake_html), \
         patch("trafilatura.extract", return_value=fake_text), \
         patch("trafilatura.extract_metadata", return_value=_FakeMetadata("A Real Title")):
        result = await preview_url("https://example.com/article")

    assert result["title"] == "A Real Title"
    assert result["snippet"] is not None
    assert len(result["snippet"]) <= 221  # 220 chars + ellipsis
    assert result["word_count"] == 250


async def test_preview_url_returns_nulls_when_page_not_fetchable():
    with patch("app.pipelines.text.scraper._guard_public_url"), \
         patch("app.pipelines.text.scraper._safe_download", return_value=None):
        result = await preview_url("https://example.com/unreachable")

    assert (result["title"], result["snippet"], result["word_count"]) == (None, None, 0)
    assert result["readable"] is False and result["problem"], "the member is told why, in words"


async def test_preview_url_rejects_invalid_format():
    with pytest.raises(ValueError, match="web address"):
        await preview_url("not-a-url")


async def test_preview_url_refuses_a_blocked_ssrf_target_without_naming_it():
    with patch(
        "app.pipelines.text.scraper._guard_public_url",
        side_effect=BlockedURLError("blocked 169.254.169.254"),
    ):
        result = await preview_url("http://169.254.169.254/latest/meta-data/")
    assert result["readable"] is False and result["title"] is None
    assert "169.254" not in (result["problem"] or ""), "the member is not told which internal address was refused"


def test_a_redirect_to_an_internal_address_is_refused(monkeypatch):
    import httpx

    from app.pipelines.text import scraper

    calls = []

    def public_then_private(url):
        calls.append(url)
        if "169.254" in url:
            raise scraper.BlockedURLError("private")

    monkeypatch.setattr(scraper, "_guard_public_url", public_then_private)

    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(scraper.UrlFetchError):
        scraper._safe_download("https://example.com/start")
    assert calls[-1].startswith("http://169.254")  # the second address was checked before it was requested
