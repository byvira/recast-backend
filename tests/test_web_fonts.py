"""Brand fonts outside the bundled twelve are fetched once from Google Fonts and reused; anything else falls back quietly.
No network: Google Fonts is a fake transport here."""

import httpx
import pytest

from app.pipelines.media import web_fonts

TTF = b"\x00\x01\x00\x00" + b"\x00" * 64
CSS = """
@font-face { font-family: 'Fira Sans'; font-weight: 400; src: url(https://fonts.gstatic.com/s/fira/r.ttf) format('truetype'); }
@font-face { font-family: 'Fira Sans'; font-weight: 700; src: url(https://fonts.gstatic.com/s/fira/b.ttf) format('truetype'); }
"""


@pytest.fixture
def fake_google(monkeypatch, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "fonts.googleapis.com":
            family = request.url.params.get("family", "")
            return httpx.Response(200, text=CSS) if family.startswith("Fira Sans") else httpx.Response(400, text="bad family")
        return httpx.Response(200, content=TTF)

    real_client = httpx.Client
    monkeypatch.setattr(web_fonts.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(web_fonts, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(web_fonts, "_failed", {})
    return calls


def test_a_google_font_is_fetched_once_and_then_read_from_disk(fake_google):
    first = web_fonts.font_files("Fira Sans")
    assert first and first["regular"].endswith("fira_sans-400.ttf") and first["bold"].endswith("fira_sans-700.ttf")
    fetched = len(fake_google)
    assert web_fonts.font_files("fira  sans") == first
    assert len(fake_google) == fetched  # the second call used the files on disk


def test_an_unknown_name_falls_back_and_is_not_retried_at_once(fake_google):
    assert web_fonts.font_files("Not A Real Font") is None
    calls = len(fake_google)
    assert web_fonts.font_files("Not A Real Font") is None
    assert len(fake_google) == calls


def test_names_with_odd_characters_are_never_sent(fake_google):
    for name in ("../etc/passwd", "Fira&Sans", "", None, "x" * 60):
        assert web_fonts.font_files(name) is None
    assert fake_google == []
