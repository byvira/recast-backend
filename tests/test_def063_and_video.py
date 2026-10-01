"""Backups (JSON repair, Deepgram transcription, pronunciation respelling, basic echo cleanup, NVIDIA) and the audio to
video quality work (presets, 4:5 size, brand layers, output quality gate). No network and no real keys."""
import asyncio
import io

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException

from app.core.config import settings
from app.shared import llm, open_fallbacks as of


def _wav(seconds: float = 2.0, freq: float = 220.0) -> bytes:
    sr = 16000
    t = np.arange(int(sr * seconds)) / sr
    buf = io.BytesIO()
    sf.write(buf, (0.3 * np.sin(2 * np.pi * freq * t)).astype("float32"), sr, format="WAV")
    return buf.getvalue()


def _mock_client(monkeypatch, module, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler), **k))


# ---- JSON repair -----------------------------------------------------------------------------------------------------------------
def test_a_backup_answer_that_is_not_json_gets_one_repair_try(monkeypatch):
    calls = []

    async def fake(prompt, system="", model=None, json_mode=False):
        calls.append((prompt, json_mode))
        return "Sure! here you go" if len(calls) == 1 else '{"hooks": ["a", "b"]}'

    monkeypatch.setattr(llm, "call_llm_fallback", fake)
    assert asyncio.run(llm.call_llm_structured_fallback("make hooks")) == {"hooks": ["a", "b"]}
    assert len(calls) == 2 and all(j for _, j in calls) and "could not be read as JSON" in calls[1][0]


def test_if_the_repair_fails_too_the_result_is_empty_not_an_error(monkeypatch):
    async def fake(prompt, system="", model=None, json_mode=False):
        if "could not be read" in prompt:
            raise HTTPException(status_code=503, detail="down")
        return "nope"

    monkeypatch.setattr(llm, "call_llm_fallback", fake)
    assert asyncio.run(llm.call_llm_structured_fallback("x")) == {}


def test_json_mode_is_sent_only_to_providers_that_accept_it(monkeypatch):
    import json

    for name, value in (("MISTRAL_API_KEY", "k"), ("OPENROUTER_API_KEY", "k"), ("NVIDIA_API_KEY", "k")):
        monkeypatch.setattr(settings, name, value)
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen[req.url.host] = "response_format" in body
        return httpx.Response(500) if req.url.host != "openrouter.ai" else httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    _mock_client(monkeypatch, of, handler)
    asyncio.run(of.open_text_fallback("hi", "", json_mode=True))
    assert seen["api.mistral.ai"] is True and seen["integrate.api.nvidia.com"] is True and seen["openrouter.ai"] is False


def test_nvidia_is_a_text_backup_only_when_it_has_a_key(monkeypatch):
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "")
    assert "nvidia" not in [p.name for p in of.text_providers()]
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "k")
    assert "nvidia" in [p.name for p in of.text_providers()]


# ---- Deepgram transcription backup -------------------------------------------------------------------------------------------------------
DG = {"results": {"channels": [{"detected_language": "en", "alternatives": [{"words": [
    {"word": "hello", "punctuated_word": "Hello,", "start": 0.0, "end": 0.4}, {"word": "world", "punctuated_word": "world.", "start": 0.5, "end": 0.9}]}]}],
    "utterances": [{"start": 0.0, "end": 0.9, "transcript": "Hello, world."}]}}


def test_deepgram_results_become_the_pipelines_words_and_segments():
    from app.pipelines.audio import deepgram_stt as dg

    assert dg.words_from(DG) == [("Hello,", 0.0, 0.4), ("world.", 0.5, 0.9)]
    assert dg.detected_language(DG) == "en"
    text, segs = dg.text_and_segments(DG)
    assert text == "Hello, world." and segs == [{"start": 0.0, "end": 0.9, "text": "Hello, world."}]
    only_words = {"results": {"channels": DG["results"]["channels"]}}
    assert dg.text_and_segments(only_words)[0] == "Hello, world."


def test_transcription_falls_back_to_deepgram_when_both_whisper_models_fail(monkeypatch):
    from app.pipelines.audio import deepgram_stt as dg, transcriber

    class Boom:
        async def create(self, **kw):
            raise RuntimeError("groq down")

    fake_groq = type("G", (), {"audio": type("A", (), {"transcriptions": Boom()})()})()
    monkeypatch.setattr(transcriber, "get_groq_client", lambda: fake_groq)
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "dg")
    _mock_client(monkeypatch, dg, lambda req: httpx.Response(200, json=DG))
    words, language = asyncio.run(transcriber.transcribe_audio_detailed(b"audio", "clip.mp3", None))
    assert [w.word for w in words] == ["Hello,", "world."] and language == "en"

    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "")
    assert asyncio.run(transcriber.transcribe_audio_detailed(b"audio", "clip.mp3", None)) == ([], None)


# ---- pronunciation respelling --------------------------------------------------------------------------------------------------------
def test_pronunciations_are_applied_as_whole_word_respelling():
    from app.models.lexicon import MemberLexicon, PronunciationEntry
    from app.pipelines.media.tts_generation import respell

    lex = MemberLexicon(id="w:u", workspace_id="w", user_id="u", pronunciations=[
        PronunciationEntry(id="1", term="Zendly", ipa="zen-dlee"), PronunciationEntry(id="2", term="SQL", ipa="sequel")])
    assert respell("Zendly runs SQL. zendly again, but not Zendlyish or MySQL.", lex) == "zen-dlee runs sequel. zen-dlee again, but not Zendlyish or MySQL."
    assert respell("plain", None) == "plain"


# ---- basic echo cleanup ---------------------------------------------------------------------------------------------------------------
def test_basic_cleanup_returns_processed_audio_and_says_what_it_is():
    from app.pipelines.media import echo_reduction as er

    out = asyncio.run(er.basic_cleanup(_wav(1.5)))
    data, rate = sf.read(io.BytesIO(out))
    assert rate == 44100 and len(data) > 44100 and "cannot remove a true room echo" in er.BASIC_CLEANUP_NOTE
    with pytest.raises(er.EchoReductionError):
        asyncio.run(er.basic_cleanup(b"not audio at all"))


# ---- video presets and the quality work -------------------------------------------------------------------------------------------------
def test_platform_advice_is_plain_and_never_blocks():
    from app.pipelines.media import video_presets as vp

    assert vp.advice(None, "square", 30) == []
    assert vp.advice("linkedin", "portrait", 45) == []
    notes = vp.advice("instagram_reels", "landscape", 240)
    joined = " ".join(notes)
    assert "vertical 9:16" in joined and "up to 3:00" in joined and "confirm" in joined
    long_but_allowed = vp.advice("linkedin", "square", 300)
    assert any("hold attention" in n for n in long_but_allowed)
    assert "—" not in joined and len(vp.presets_payload()) == len(vp.PRESETS)


def test_captions_are_bigger_and_clear_of_the_platform_buttons_on_tall_video():
    from app.pipelines.media.video_render import caption_layout

    tall_font, tall_margin, tall_words = caption_layout(1080, 1920)
    sq_font, sq_margin, sq_words = caption_layout(1080, 1080)
    assert tall_font > sq_font and tall_margin >= int(1920 * 0.2) and tall_words < sq_words
    assert caption_layout(1080, 1350)[1] == int(1350 * 0.14)


def test_the_title_and_logo_are_a_separate_layer_so_only_the_background_moves():
    from PIL import Image

    from app.pipelines.media.video_compose import BrandLook, compose_layers

    logo = io.BytesIO()
    Image.new("RGBA", (200, 80), (255, 0, 0, 255)).save(logo, "PNG")
    bg, text = compose_layers(size=(540, 960), size_name="vertical", style="waveform", look=BrandLook(accent_hex="#f59e0b"), title="Hello there", logo_bytes=logo.getvalue())
    assert Image.open(io.BytesIO(bg)).size == (540, 960) and text is not None
    layer = Image.open(io.BytesIO(text)).convert("RGBA")
    assert layer.getbbox() is not None and layer.getpixel((5, 5))[3] == 0  # transparent except where the logo and title are
    _, none = compose_layers(size=(540, 960), size_name="vertical", style="solid", look=BrandLook(), title="", logo_bytes=None)
    assert none is None


def test_a_portrait_video_passes_the_quality_gate_and_has_the_standard_encoding():
    from app.pipelines.media.video_render import TranscriptWordLike, render_video

    words = [TranscriptWordLike(word=f"w{i}", start_s=i * 0.4, end_s=i * 0.4 + 0.3) for i in range(5)]
    out = asyncio.run(render_video(
        audio_bytes=_wav(2.0), words=words, start_s=0.0, end_s=2.0, style="waveform", size="portrait",
        background_hex="#0f172a", accent_hex="#f59e0b", primary_hex="#312e81", title="A short title",
    ))
    assert out[4:8] == b"ftyp"
    import imageio_ffmpeg
    import subprocess

    info = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", "-"], input=out, capture_output=True).stderr.decode(errors="replace")
    assert "1080x1350" in info and "h264 (High)" in info and "30 fps" in info and "aac" in info and "48000 Hz" in info and "bt709" in info


def test_the_quality_gate_rejects_a_broken_file(tmp_path):
    from app.pipelines.media.video_render import VideoRenderError, verify_output

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"junk" * 50)
    with pytest.raises(VideoRenderError, match="quality check"):
        asyncio.run(verify_output(bad, 2.0, 1080, 1080))
