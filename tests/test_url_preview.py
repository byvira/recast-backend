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
         patch("trafilatura.fetch_url", return_value=fake_html), \
         patch("trafilatura.extract", return_value=fake_text), \
         patch("trafilatura.extract_metadata", return_value=_FakeMetadata("A Real Title")):
        result = await preview_url("https://example.com/article")

    assert result["title"] == "A Real Title"
    assert result["snippet"] is not None
    assert len(result["snippet"]) <= 221  # 220 chars + ellipsis
    assert result["word_count"] == 250


async def test_preview_url_returns_nulls_when_page_not_fetchable():
    with patch("app.pipelines.text.scraper._guard_public_url"), \
         patch("trafilatura.fetch_url", return_value=None):
        result = await preview_url("https://example.com/unreachable")

    assert result == {"title": None, "snippet": None, "word_count": 0}


async def test_preview_url_rejects_invalid_format():
    with pytest.raises(ValueError, match="Invalid URL format"):
        await preview_url("not-a-url")


async def test_preview_url_raises_on_blocked_ssrf_target():
    with patch(
        "app.pipelines.text.scraper._guard_public_url",
        side_effect=BlockedURLError("blocked"),
    ):
        with pytest.raises(ValueError, match="blocked"):
            await preview_url("http://169.254.169.254/latest/meta-data/")
