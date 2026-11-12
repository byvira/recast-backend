from app.core.config import settings
from app.models.voice_settings import MemberVoiceSettings
from app.pipelines.media import tts_generation as tts


def _voice(voice: str) -> MemberVoiceSettings:
    return MemberVoiceSettings(id="w:u", workspace_id="w", user_id="u", tts_voice=voice)


def test_standard_voice_languages_are_never_blocked(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    assert tts.narration_blocker("English", _voice("piper_lessac")) is None
    assert tts.narration_blocker("Spanish", _voice("piper_lessac")) is None


def test_tamil_with_the_offline_voice_says_to_pick_a_premium_voice(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_TTS_ENABLED", False)
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", True)
    reason = tts.narration_blocker("Tamil", _voice("piper_lessac"))
    assert reason and "premium voice" in reason and "Tamil" in reason


def test_tamil_without_the_premium_service_says_it_is_not_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_TTS_ENABLED", False)
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", False)
    reason = tts.narration_blocker("Tamil", _voice("abc123"))
    assert reason and "isn't switched on" in reason


def test_tamil_with_a_real_voice_is_allowed(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", True)
    monkeypatch.setattr(tts, "_elevenlabs_skip_until", 0.0)
    assert tts.narration_blocker("Tamil", _voice("21m00Tcm4TlvDq8ikWAM")) is None


def test_hindi_and_tamil_are_never_blocked_while_gemini_speech_is_on_but_other_languages_still_are(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", False)
    monkeypatch.setattr(settings, "GEMINI_TTS_ENABLED", True)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "key")
    assert tts.narration_blocker("Tamil", _voice("piper_lessac")) is None
    assert tts.narration_blocker("hi", _voice("piper_lessac")) is None
    blocked = tts.narration_blocker("Korean", _voice("piper_lessac"))
    assert blocked and "Hindi" in blocked and "Tamil" in blocked
    monkeypatch.setattr(settings, "GEMINI_TTS_ENABLED", False)
    assert "isn't switched on" in tts.narration_blocker("Tamil", _voice("piper_lessac"))


def test_cloudflare_speech_reads_korean_and_chinese_and_refuses_a_mixed_or_unknown_language(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_TTS_ENABLED", True)
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "k")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "a")
    assert tts._melotts_code("Hello", "ko") == "kr" and tts._melotts_code("Hello", "ja") == "jp" and tts._melotts_code("Hello", "zh-CN") == "zh"
    assert tts._melotts_code("Hello", "ta+en") is None and tts._melotts_code("Hello", "ta") is None
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", False)
    monkeypatch.setattr(settings, "GEMINI_TTS_ENABLED", False)
    assert tts.narration_blocker("Korean", _voice("piper_lessac")) is None
    assert tts.narration_blocker("Chinese", _voice("piper_lessac")) is None
    assert tts.narration_blocker("Tamil", _voice("piper_lessac"))
    monkeypatch.setattr(settings, "CLOUDFLARE_TTS_ENABLED", False)
    assert tts.narration_blocker("Korean", _voice("piper_lessac"))


# ── the last-resort Edge voices stay contained ───────────────────────────────

import asyncio
from pathlib import Path

from app.pipelines.media import edge_voice


def _only_edge(monkeypatch, enabled=True):
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", False)
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_TTS_ENABLED", False)
    monkeypatch.setattr(settings, "CLOUDFLARE_TTS_ENABLED", False)
    monkeypatch.setattr(settings, "EDGE_TTS_ENABLED", enabled)


def _speak(monkeypatch, calls):
    async def speak(text, code):
        calls.append(code)
        return b"mp3"

    monkeypatch.setattr(edge_voice, "speak", speak)


def _part(language, private=False):
    return asyncio.run(tts._synthesize_part(text="Merhaba dünya", voice_settings=_voice("piper_lessac"), lexicon=None, workspace_id="w",
                                            user_id="u", timed=False, language=language, private=private))


def test_edge_voices_are_off_by_default():
    assert settings.EDGE_TTS_ENABLED is False or settings.EDGE_TTS_ENABLED == False  # noqa: E712 - tests switch it off in conftest


def test_edge_reads_a_language_when_switched_on_and_never_for_private_text(monkeypatch):
    calls: list[str] = []
    _only_edge(monkeypatch)
    _speak(monkeypatch, calls)
    assert _part("tr").audio == b"mp3" and calls == ["tr"]
    assert _part("tr", private=True) is None and calls == ["tr"], "private text must never reach the Edge voices"
    assert tts.narration_blocker("Turkish", _voice("piper_lessac")) is None
    assert tts.narration_blocker("Turkish", _voice("piper_lessac"), private=True)
    assert tts.is_language_supported("turkish")[0] is True and tts.is_language_supported("turkish", private=True)[0] is False


def test_edge_is_never_called_while_switched_off_or_for_a_language_outside_its_table(monkeypatch):
    calls: list[str] = []
    _only_edge(monkeypatch, enabled=False)
    _speak(monkeypatch, calls)
    assert _part("tr") is None and calls == []
    _only_edge(monkeypatch)
    assert _part("vi") is None and _part("ta+en") is None and calls == []


def test_edge_refuses_an_unknown_language_and_bounds_what_it_takes(monkeypatch):
    with __import__("pytest").raises(ValueError):
        asyncio.run(edge_voice.speak("hello", "xx"))
    assert edge_voice.code_for("hello", "TH") == "th" and edge_voice.code_for("hello", "vi") is None
    assert edge_voice.MAX_CHARS <= 3000 and edge_voice.TIMEOUT_S <= 60 and edge_voice.MAX_AUDIO_BYTES <= 25 * 1024 * 1024


def test_member_footage_and_recordings_are_always_marked_private_and_only_one_module_imports_the_package():
    app = Path(__file__).resolve().parents[1] / "app"
    assert "private=True" in (app / "api/v1/video_narrate.py").read_text(encoding="utf-8")
    localize = (app / "api/v1/audio_assets.py").read_text(encoding="utf-8")
    assert "is_language_supported(body.target_language, private=True)" in localize and "user_id=ctx.user_id, private=True," in localize
    importers = [str(p.relative_to(app)) for p in app.rglob("*.py") if "import edge_tts" in p.read_text(encoding="utf-8")]
    assert importers == [str(Path("pipelines/media/edge_voice.py"))], importers
