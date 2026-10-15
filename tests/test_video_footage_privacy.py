"""Text taken from a member's own footage goes to the main provider and one second host (Cloudflare), and never to Gemini, Mistral, NVIDIA or
OpenRouter. The providers are replaced by recorders so the order and the refusals are what is checked."""
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.shared import llm, open_fallbacks


@pytest.fixture
def recorder(monkeypatch):
    calls: list[str] = []

    def gemini_client():
        calls.append("gemini")
        raise RuntimeError("gemini must not be reached")

    async def open_text(prompt, system="", json_mode=False):
        calls.append("open")
        return "from an open provider"

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(llm, "get_gemini_client", gemini_client)
    monkeypatch.setattr(open_fallbacks, "open_text_fallback", open_text)
    return calls


async def test_footage_text_goes_to_cloudflare_and_to_nothing_else(recorder, monkeypatch):
    async def cloudflare(prompt, system="", json_mode=False):
        recorder.append("cloudflare")
        return '{"ok": true}'

    monkeypatch.setattr(open_fallbacks, "cloudflare_text", cloudflare)
    async with llm.member_footage():
        out = await llm.call_llm_fallback("what was said in the video", json_mode=True)
    assert out == '{"ok": true}'
    assert recorder == ["cloudflare"]


async def test_when_cloudflare_cannot_answer_the_call_fails_and_no_other_provider_is_tried(recorder, monkeypatch):
    async def cloudflare(prompt, system="", json_mode=False):
        recorder.append("cloudflare")
        return None

    monkeypatch.setattr(open_fallbacks, "cloudflare_text", cloudflare)
    async with llm.member_footage():
        with pytest.raises(HTTPException) as err:
            await llm.call_llm_fallback("what was said in the video")
    assert err.value.status_code == 503
    assert recorder == ["cloudflare"]


async def test_the_flag_is_released_after_the_block_and_support_text_still_never_reaches_cloudflare(recorder, monkeypatch):
    async def cloudflare(prompt, system="", json_mode=False):
        recorder.append("cloudflare")
        return "x"

    monkeypatch.setattr(open_fallbacks, "cloudflare_text", cloudflare)
    async with llm.private_content():
        with pytest.raises(HTTPException):
            await llm.call_llm_fallback("a support message")
    assert recorder == []
    assert llm._private_content.get() is False and llm._footage_content.get() is False


async def test_cloudflare_is_not_called_without_its_settings(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "")
    assert await open_fallbacks.cloudflare_text("hello") is None


def test_every_prompt_the_video_pipeline_makes_on_footage_goes_through_the_guard():
    import inspect

    from app.api.v1 import video_narrate
    from app.video.analysis import analyze
    from app.video.create import generate

    for fn in (generate.slot_caller, analyze._default_caller, video_narrate.start_narration):
        assert "member_footage" in inspect.getsource(fn), fn.__name__
